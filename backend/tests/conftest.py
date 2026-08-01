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
#
# Il percorso e' relativo alla working directory: due run pytest nella STESSA
# directory si cancellano lo schema a vicenda col drop_all di sessione, e
# producono rossi che sembrano regressioni (28/07: tre run con 24, poi 1, poi
# 22 falliti, tutti fantasmi, contro 729 verdi in isolamento). Con lo slot,
# due run possono convivere dichiarando WA_TEST_DB_SLOT diversi; senza slot,
# il lock qui sotto fa fallire subito il secondo con un messaggio chiaro
# invece di lasciarlo sbagliare in silenzio (contratto M2/M3 §8.1).
_SLOT = os.environ.get("WA_TEST_DB_SLOT", "default")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///./data/test_bot_{_SLOT}.db"

# La cartella e' gitignorata: in locale esiste perche' ci gira il bot, su un
# runner pulito no, e sqlite non la crea da solo ("unable to open database
# file", 700+ ERROR in setup che sembrano un guasto della suite e sono una
# cartella mancante).
os.makedirs("data", exist_ok=True)

_LOCK_PATH = f"./data/test_bot_{_SLOT}.lock"

import asyncio  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from app.services import notifier  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _suite_lock():
    """Un solo run pytest alla volta per slot. Il file resta se un run viene
    ucciso: si cancella a mano, ed e' comunque meglio di un'ora persa a
    inseguire rossi fantasma."""
    try:
        fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError(
            f"Un'altra suite pytest sta girando sullo slot '{_SLOT}' "
            f"({_LOCK_PATH}). Usa WA_TEST_DB_SLOT=<nome> per uno slot tuo, "
            "oppure cancella il file se e' rimasto da un run ucciso."
        )
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    try:
        yield
    finally:
        try:
            os.remove(_LOCK_PATH)
        except OSError:
            pass


@pytest.fixture(scope="session", autouse=True)
def _init_test_db():
    """Crea lo schema sul DB di test una volta per sessione (engine usa e getta
    nel proprio loop → nessun problema cross-loop con l'engine dell'app)."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from app.config import settings
    from app.database import Base
    from app.utils.db_dialect import to_async_database_url
    import app.models  # noqa: F401 - registra tutti i modelli in Base

    async def _create():
        eng = create_async_engine(to_async_database_url(settings.database_url))
        async with eng.begin() as conn:
            # drop+create: ogni run della suite parte da uno schema PULITO (il file
            # sqlite persiste tra run; senza il drop i dati si accumulano e i test con
            # valori unique fissi collidono / i conteggi driftano).
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        await eng.dispose()

    asyncio.run(_create())
    yield


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch):
    monkeypatch.setattr(notifier, "_telegram_enabled", lambda: False)


@pytest_asyncio.fixture
async def db_session():
    """Sessione su SQLite di test, con rollback a fine test: nessun test
    vede le scritture di un altro."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.config import settings
    from app.utils.db_dialect import to_async_database_url

    eng = create_async_engine(to_async_database_url(settings.database_url))
    maker = async_sessionmaker(eng, expire_on_commit=False)
    async with maker() as s:
        yield s
        await s.rollback()
    await eng.dispose()
