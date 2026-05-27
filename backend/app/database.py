from uuid import uuid4

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool
from sqlalchemy import text
from loguru import logger
from app.config import settings
from app.utils.db_dialect import is_postgres, is_sqlite, to_async_database_url


def _connect_args() -> dict:
    if is_sqlite(settings.database_url):
        return {"check_same_thread": False, "timeout": 30}
    if is_postgres(settings.database_url):
        return {
            "statement_cache_size": 0,
            "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
        }
    return {}


_engine_kwargs = {
    "echo": False,
    "connect_args": _connect_args(),
}
if is_postgres(settings.database_url):
    _engine_kwargs["poolclass"] = NullPool

engine = create_async_engine(
    to_async_database_url(settings.database_url),
    **_engine_kwargs,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def setup_pragmas():
    """Set SQLite performance/safety pragmas. Called once at startup before migrations."""
    if not is_sqlite(settings.database_url):
        logger.info("Database pragmas skipped (non-SQLite backend)")
        return
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA synchronous=NORMAL"))
    logger.info("SQLite pragmas set (WAL, synchronous=NORMAL)")
