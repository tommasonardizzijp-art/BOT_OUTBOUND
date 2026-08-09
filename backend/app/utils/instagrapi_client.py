"""
Shared instagrapi login utility.

Per-account asyncio lock prevents scraper and reply_checker from
simultaneously restoring the same Instagram session. Concurrent logins
on the same account can trigger challenge detection on Instagram's side,
especially during long scraping runs (>1h) that overlap with the 30-min
reply_checker cron.
"""
import asyncio
import json
from datetime import datetime
from loguru import logger

from app.models.account import InstagramAccount, AccountStatus
from app.utils.exceptions import ScraperError, AccountChallengeError


def _patch_media_xma() -> None:
    """Make MediaXma.video_url optional.

    Instagram sometimes returns null for video_url inside XMA media (reels,
    stories, link previews). instagrapi declares the field as required HttpUrl,
    so pydantic raises ValidationError and direct_threads() crashes for any
    account whose inbox contains such a thread.

    Patching at module load time (before any Client is created) covers all
    callers — recovery_checker, reply_checker, scraper.

    Strategy: mutate the EXISTING MediaXma class in place (make video_url
    optional) and rebuild it + DirectMessage/ReplyMessage. We must NOT subclass:
    instagrapi.extractors captured `from .types import MediaXma` at its own import
    time and builds instances via that reference (extract_media_v1_xma →
    MediaXma(...)). A subclass left those extractor-built instances as the ORIGINAL
    class, which then failed DirectMessage validation with
    'Input should be a valid dictionary or instance of _PatchedMediaXma'
    (type-identity mismatch) — crashing direct_threads() for ANY thread containing
    an xma_share, video_url null or not. Mutating in place keeps a single class
    object shared by types + extractors, so every instance validates.
    """
    try:
        from typing import Optional
        from pydantic import HttpUrl
        from pydantic_core import PydanticUndefined
        import instagrapi.types as _t

        if not hasattr(_t, "MediaXma"):
            return

        field = _t.MediaXma.model_fields.get("video_url")
        if field is None or not field.is_required():
            return  # already optional — nothing to do

        # Mutate the field on the original class: Optional annotation + None default
        # (default != PydanticUndefined ⇒ is_required() becomes False).
        field.annotation = Optional[HttpUrl]
        field.default = None
        _t.MediaXma.model_rebuild(force=True)

        # Rebuild dependent models so their compiled xma_share validators pick up
        # MediaXma's new (None-tolerant) core schema. Annotation stays the same
        # class object — only its schema changed.
        ns = vars(_t)
        for model_name in ("DirectMessage", "ReplyMessage"):
            model = ns.get(model_name)
            if model is not None:
                model.model_rebuild(force=True, _types_namespace=ns)

        logger.debug("[Patch] instagrapi MediaXma.video_url → Optional[HttpUrl] (in-place mutate + rebuild)")
    except Exception as exc:
        logger.warning(f"[Patch] MediaXma patch skipped (non-fatal): {exc}")


_patch_media_xma()

# Per-account lock: prevents concurrent session restores for same account
_account_locks: dict[str, asyncio.Lock] = {}
_locks_dict_mutex = asyncio.Lock()

# Scraping slot: prevents two ARQ jobs scraping with the same account concurrently.
# Risk without this: 2x API call rate on same session + race condition on session_data writes.
_scraping_accounts: set[str] = set()
_scraping_set_lock = asyncio.Lock()


async def acquire_scraping_slot(account_id: str) -> bool:
    """Claim account for scraping. Returns False if already claimed by another job."""
    async with _scraping_set_lock:
        if account_id in _scraping_accounts:
            return False
        _scraping_accounts.add(account_id)
        return True


async def release_scraping_slot(account_id: str) -> None:
    async with _scraping_set_lock:
        _scraping_accounts.discard(account_id)


def get_scraping_account_ids() -> frozenset[str]:
    """Snapshot of accounts currently in use for scraping."""
    return frozenset(_scraping_accounts)


async def _get_account_lock(account_id: str) -> asyncio.Lock:
    async with _locks_dict_mutex:
        if account_id not in _account_locks:
            _account_locks[account_id] = asyncio.Lock()
        return _account_locks[account_id]


async def login(account: InstagramAccount, db) -> "Client":
    """
    Restore Instagram session for `account`.
    Acquires per-account lock — only one caller at a time can restore
    the same account's session, preventing concurrent login races.
    NEVER attempts automated fresh login (ban risk). Requires prior
    manual browser login via Account → 'Login Browser'.

    Il ripristino non fa nessuna chiamata di verifica: vedi _do_login per il
    perche' (la vecchia verifica GQL partiva anonima e prendeva 429 sempre).
    """
    lock = await _get_account_lock(account.id)
    async with lock:
        return await _do_login(account, db)


async def _do_login(account: InstagramAccount, db) -> "Client":
    from instagrapi import Client

    client = Client()

    if account.proxy:
        client.set_proxy(account.proxy)

    # Fail-fast su QUALSIASI chiamata alla sessione `public`. instagrapi monta di
    # default una Retry(total=3, backoff_factor=2) su `public`: ogni risposta
    # 429/5xx costa ~15s di ritentativi. Riduciamo a 1 retry senza backoff, cosi'
    # un rifiuto torna in ~1-2s invece di bloccare il chiamante.
    # (Nato per il verify GQL, ora rimosso — vedi sotto. Resta perche' vale per
    # ogni altro uso della sessione web, e meno richieste ripetute = meno
    # aggressivo, non di piu'.)
    try:
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        _fast_retry = HTTPAdapter(max_retries=Retry(
            total=1, backoff_factor=0, status_forcelist=[429, 500, 502, 503, 504],
        ))
        client.public.mount("https://", _fast_retry)
        client.public.mount("http://", _fast_retry)
    except Exception as _retry_exc:
        logger.debug(f"fast-retry public non applicato per @{account.username}: {_retry_exc}")

    if not account.session_data:
        raise ScraperError(
            f"@{account.username} non ha una sessione salvata. "
            "Vai su Account → 'Login Browser' per effettuare il login manuale "
            "PRIMA di avviare una campagna."
        )

    try:
        session = json.loads(account.session_data)
        client.set_settings(session)

        # NESSUNA verifica GQL. Qui c'era una chiamata a user_info_by_username_gql
        # che doveva "far riposare" la sessione web prima del salto agli endpoint
        # mobile (per evitare la challenge UFAC dopo un login manuale via browser).
        # Non ha mai fatto quel lavoro: user_info_by_username_gql passa da
        # `client.public`, una requests.Session distinta da `client.private`, e i
        # cookie della sessione salvata finiscono SOLO in `private` (instagrapi
        # auth.py::init). `public` riceve il sessionid unicamente via
        # inject_sessionid_to_public(), che instagrapi chiama dentro login() — e
        # noi login() non lo facciamo mai (per scelta anti-ban: si ripristina e
        # basta). Quindi la richiesta partiva ANONIMA, e Instagram risponde 429 a
        # web_profile_info anonimo: da qui il "GQL verify 429" a ogni login, su
        # ogni account, anche appena creato, da qualsiasi IP.
        # Essendo anonima non toccava la sessione dell'account: non preparava
        # nulla. Restava solo il costo — 1-2s per login e una richiesta anonima
        # verso web_profile_info, l'endpoint piu' sorvegliato che tocchiamo
        # (misurato 0/65 contro 65/65 del GraphQL passivo). Rimossa.
        account.session_data = json.dumps(client.get_settings())
        account.last_login_at = datetime.utcnow()
        await db.commit()

        logger.info(f"Sessione ripristinata per @{account.username}")
        return client

    except Exception as e:
        # Catch ALL challenge variants (ChallengeRequired, ChallengeUnknownStep,
        # and any future step_name Instagram may add) by checking the exception
        # class name — instagrapi uses a flat hierarchy where all challenge errors
        # inherit from ClientError but their names consistently contain "Challenge".
        exc_name = type(e).__name__
        if "Challenge" in exc_name:
            account.status = AccountStatus.challenge_required
            await db.commit()
            logger.warning(
                f"Challenge rilevata per @{account.username} ({exc_name}) — "
                "account marcato challenge_required. Login manuale richiesto."
            )
            raise AccountChallengeError(account.id, str(e))

        logger.error(
            f"Ripristino sessione fallito per @{account.username}: "
            f"{exc_name}: {e}"
        )
        raise ScraperError(
            f"La sessione di @{account.username} è scaduta o non valida. "
            "Vai su Account → 'Login Browser' per rifare il login manuale. "
            f"(Errore: {exc_name})"
        )
