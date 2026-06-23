from contextlib import asynccontextmanager
from datetime import datetime
import os
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from loguru import logger
from app.config import settings
from app.database import setup_pragmas, AsyncSessionLocal
from app.models.account import InstagramAccount
from app.models.message import Message, MessageStatus
from sqlalchemy import select, func
from app.api import accounts, campaigns, campaign_accounts, followers, messages, dashboard, health, leads, anomalies, auth, admin, ops, lead_qualification
from fastapi import Depends
from app.utils.auth_deps import get_current_user


async def _sync_daily_message_counts():
    """Sync account.daily_message_count from the messages table.
    Runs at boot so stale counters (cron missed at midnight) are corrected."""
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    async with AsyncSessionLocal() as db:
        accs = (await db.execute(select(InstagramAccount))).scalars().all()
        for acc in accs:
            live_count = await db.scalar(
                select(func.count(Message.id)).where(
                    Message.account_id == acc.id,
                    Message.status == MessageStatus.sent,
                    Message.sent_at >= today_start,
                )
            ) or 0
            if acc.daily_message_count != live_count:
                logger.info(f"[Boot] @{acc.username} daily_count corrected: {acc.daily_message_count} → {live_count}")
                acc.daily_message_count = live_count
        await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure data directory exists
    os.makedirs("data", exist_ok=True)
    os.makedirs(settings.browser_profiles_dir, exist_ok=True)

    logger.info("Initializing database...")
    await setup_pragmas()
    logger.info("Migrazioni NON eseguite al boot - lanciare 'python -m scripts.migrate' nel deploy")
    await _sync_daily_message_counts()
    from app.services.account_manager import advance_warmup_if_needed
    await advance_warmup_if_needed()

    # BUG-NEW-34: warn if .env has test/aggressive timing values
    if settings.min_delay_seconds < 60:
        logger.warning(
            f"⚠️  MIN_DELAY_SECONDS={settings.min_delay_seconds}s — valore da test aggressivo! "
            "In produzione usare ≥120s per evitare ban. Aggiorna .env prima di usare con account reali."
        )
    if settings.session_min_messages < 8:
        logger.warning(
            f"⚠️  SESSION_MIN_MESSAGES={settings.session_min_messages} — valore da test. "
            "In produzione usare ≥10."
        )

    logger.info("BOT OUTBOUND backend started")
    yield
    logger.info("BOT OUTBOUND backend shutting down")


app = FastAPI(
    title="BOT OUTBOUND API",
    description="Instagram outbound DM automation backend",
    version="0.1.0",
    lifespan=lifespan,
)

class _CatchUnhandledMiddleware(BaseHTTPMiddleware):
    """Converte ogni eccezione non gestita in una JSONResponse 500.

    Senza questo, un'eccezione propagava fino a ServerErrorMiddleware (il layer
    piu' esterno di Starlette, FUORI dal CORSMiddleware): il 500 risultante non
    passava piu' dal CORS, quindi arrivava al browser SENZA header
    `Access-Control-Allow-Origin`. Il frontend vedeva un errore CORS opaco /
    "Failed to load resource" invece del messaggio gestito "Backend non
    raggiungibile". Catturando qui (layer INTERNO al CORS, perche' aggiunto
    prima), la risposta 500 riattraversa il CORSMiddleware e porta gli header.
    """

    async def dispatch(self, request: Request, call_next):
        try:
            return await call_next(request)
        except Exception:
            logger.exception(f"[API] Unhandled error on {request.method} {request.url.path}")
            return JSONResponse(
                status_code=500,
                content={"detail": "Errore interno temporaneo del server. Riprova tra qualche secondo."},
            )


# IMPORTANTE: l'ordine conta. add_middleware mette il middleware piu' esterno per
# ultimo, quindi CORSMiddleware va aggiunto DOPO _CatchUnhandledMiddleware per
# avvolgerlo e aggiungere gli header CORS anche alle risposte d'errore.
app.add_middleware(_CatchUnhandledMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# When JWT_SECRET is set in .env, all routes below require a valid Bearer token.
# When JWT_SECRET is empty, get_current_user returns a synthetic admin (legacy mode).
# auth.router (login) and health.router stay open — never gated.
_protected = [Depends(get_current_user)]

app.include_router(accounts.router, prefix="/api", dependencies=_protected)
app.include_router(campaigns.router, prefix="/api", dependencies=_protected)
app.include_router(campaign_accounts.router, prefix="/api", dependencies=_protected)
app.include_router(followers.router, prefix="/api", dependencies=_protected)
app.include_router(messages.router, prefix="/api", dependencies=_protected)
app.include_router(dashboard.router, prefix="/api", dependencies=_protected)
app.include_router(health.router, prefix="/api")
app.include_router(leads.router, prefix="/api", dependencies=_protected)
app.include_router(lead_qualification.router, prefix="/api", dependencies=_protected)
app.include_router(anomalies.router, prefix="/api", dependencies=_protected)
app.include_router(ops.router, prefix="/api", dependencies=_protected)
app.include_router(admin.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(auth.users_router, prefix="/api")  # users_router has its own require_admin guards


@app.get("/")
async def root():
    return {"message": "BOT OUTBOUND API", "docs": "/docs"}
