from uuid import uuid4
from collections import namedtuple
import platform
import sys

if sys.platform.startswith("win"):
    _UnameResult = namedtuple("uname_result", "system node release version machine processor")
    platform.machine = lambda: "AMD64"
    platform.uname = lambda: _UnameResult("Windows", "", "", "", "AMD64", "AMD64")

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from loguru import logger
from app.config import settings
from app.utils.db_dialect import is_postgres, is_sqlite, to_async_database_url


@event.listens_for(Engine, "connect")
def _sqlite_enforce_foreign_keys(dbapi_connection, connection_record) -> None:
    """Attiva PRAGMA foreign_keys=ON su OGNI connessione SQLite, qualunque
    engine l'abbia aperta. Registrato sulla classe Engine (non su una singola
    istanza) perche' i test si creano engine propri (tests/conftest.py) mai
    derivati dall'`engine` qui sotto: un listener sulla sola istanza modulo
    non li coprirebbe.

    No-op per Postgres (produzione): il modulo del DBAPI adapter non contiene
    "sqlite" (asyncpg), quindi la PRAGMA non viene mai eseguita li'. Il canale
    Instagram gira solo su Postgres in produzione -- zero rischio.

    Decisione 27/07 (qa-wa-m1-adversarial.md SS K): senza questo, SQLite non
    applica MAI le foreign key. Un insert con tenant_id/campaign_id inesistente
    passava nei test e sarebbe stato rifiutato da Postgres in produzione --
    qualunque test di integrita' referenziale era cieco.
    """
    if "sqlite" not in type(dbapi_connection).__module__:
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _connect_args() -> dict:
    if is_sqlite(settings.database_url):
        return {"check_same_thread": False, "timeout": 30}
    if is_postgres(settings.database_url):
        return {
            "statement_cache_size": 0,
            "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
            # Connect timeout corto: un blip di rete verso il pooler Supabase
            # fallisce in ~15s invece di restare appeso fino al default (60s).
            # I worker convertono il fallimento in Retry(defer) (db_resilience).
            "timeout": 15,
        }
    return {}


_engine_kwargs = {
    "echo": False,
    "connect_args": _connect_args(),
}
if is_postgres(settings.database_url):
    # Bounded REUSED pool verso il pooler Supabase (transaction-mode, 6543).
    # Prima era NullPool: ogni richiesta apriva una connessione nuova (~1.8s di
    # setup verso il pooler) senza riuso. Sotto burst (la pagina campagna spara
    # ~8 richieste in parallelo + il polling sidebar) le connessioni si
    # serializzavano fino a >15s, sforando il connect timeout di asyncpg → 500
    # (che, saltando il CORSMiddleware, arrivava al browser come errore CORS opaco).
    # La compat con pgbouncer transaction-mode resta garantita dai connect_args
    # (statement_cache_size=0 + prepared_statement_name_func unico): il pool lato
    # client riusa la connessione client↔pgbouncer, pgbouncer multiplexa lato server
    # per-transazione. pool_pre_ping scarta connessioni chiuse dal pooler.
    _engine_kwargs.update(
        pool_size=10,
        max_overflow=10,
        pool_timeout=20,
        pool_recycle=1800,
        pool_pre_ping=True,
    )

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
