import httpx
from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text
from app.config import settings
from app.database import AsyncSessionLocal

router = APIRouter(prefix="/health", tags=["health"])


class HealthStatus(BaseModel):
    status: str
    ollama: str
    redis: str
    database: str


@router.get("", response_model=HealthStatus)
async def health_check():
    ai_ok = await _check_ai_provider()
    redis_ok = await _check_redis()
    db_ok = await _check_database()

    overall = "ok" if ai_ok and redis_ok and db_ok else "degraded"
    return HealthStatus(
        status=overall,
        ollama="ok" if ai_ok else "unreachable",
        redis="ok" if redis_ok else "unreachable",
        database="ok" if db_ok else "unreachable",
    )


async def _check_ai_provider() -> bool:
    provider = settings.ai_provider.lower()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            if provider == "ollama":
                resp = await client.get(f"{settings.ollama_base_url}/api/tags")
                return resp.status_code == 200
            if provider in ("groq", "openai"):
                if not settings.ai_api_key:
                    return False
                base = settings.ai_base_url.strip() or "https://api.groq.com/openai/v1"
                resp = await client.get(
                    f"{base}/models",
                    headers={"Authorization": f"Bearer {settings.ai_api_key}"},
                )
                return resp.status_code == 200
            if provider == "gemini":
                if not settings.ai_api_key:
                    return False
                resp = await client.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    params={"key": settings.ai_api_key},
                )
                return resp.status_code == 200
    except Exception:
        return False
    return False


async def _check_database() -> bool:
    """BUG-NEW-07: actually test the DB connection instead of hardcoding 'ok'."""
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _check_redis() -> bool:
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.redis_url, socket_connect_timeout=3)
        await r.ping()
        await r.aclose()
        return True
    except Exception:
        return False
