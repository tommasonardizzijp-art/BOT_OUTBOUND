"""Guardie globali di test. DUE cose, entrambe per non toccare la produzione:

1) DB → SQLite locale, MAI Supabase prod.
   Bug (07/07/2026): molti test usano `AsyncSessionLocal` (agganciato a
   settings.database_url = Supabase PROD nel .env) e creavano Campaign/Follower
   VERI in produzione a OGNI run pytest — decine di campagne-fantasma 't'/'advqreg'
   che il worker live poi tentava di processare (alert Telegram). Qui forziamo
   DATABASE_URL a uno SQLite locale PRIMA di importare qualsiasi modulo `app.*`,
   così engine/AsyncSessionLocal nascono già puntati al DB di test. Le tabelle
   vengono create una volta per sessione.

2) Telegram → spento.
   Un test che esercita il notifier senza mock spammerebbe l'operatore.
"""
import os

# DEVE stare PRIMA di ogni import `app.*`: app.config.settings viene istanziato
# al primo import e legge questa env (le env var vincono sul .env in pydantic).
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./data/test_bot.db"

import asyncio  # noqa: E402

import pytest  # noqa: E402

from app.services import notifier  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _init_test_db():
    """Crea lo schema sul DB di test una volta per sessione (engine usa e getta
    nel proprio loop → nessun problema cross-loop con l'engine dell'app)."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from app.config import settings
    from app.database import Base
    from app.utils.db_dialect import to_async_database_url

    async def _create():
        eng = create_async_engine(to_async_database_url(settings.database_url))
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await eng.dispose()

    asyncio.run(_create())
    yield


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch):
    monkeypatch.setattr(notifier, "_telegram_enabled", lambda: False)
