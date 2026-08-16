"""Bootstrap comune per gli script adversarial della famiglia CONCORRENZA
(docs/superpowers/qa/2026-08-15-qa-wa-discover-lancio-adversarial.md, sez. A).

Vanno eseguiti a mano, mai in CI: aprono piu' sessioni DB/Redis reali e sono
lenti apposta (e' il punto, riproducono race vere). Non sono test pytest per
lo stesso motivo per cui backend/tests/conftest.py isola con lo slot: due run
concorrenti sullo stesso DB si darebbero fastidio a vicenda.

DEVE essere importato PRIMA di qualunque modulo app.*: fissa DATABASE_URL e
REDIS_URL su uno slot dedicato "qa_adv" (stesso meccanismo a env-var di
tests/conftest.py, ma slot proprio per non collidere ne' con la suite pytest
ne', ovviamente, con la produzione). db Redis 14: libero, mai usato da
produzione (db 0) ne' dagli slot di test standard (10-15, vedi conftest).
"""
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_DIR))

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./data/test_bot_qa_adv.db"
os.environ["REDIS_URL"] = os.environ.get("WA_QA_ADV_REDIS_URL", "redis://localhost:6379/14")

os.makedirs(BACKEND_DIR / "data", exist_ok=True)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


async def crea_schema_pulito() -> None:
    """drop+create: ogni script parte da uno schema pulito, stesso motivo di
    _init_test_db in conftest.py (niente dati accumulati da run precedenti
    che falsano un conteggio o fanno collidere una UNIQUE)."""
    from sqlalchemy.ext.asyncio import create_async_engine

    import app.models  # noqa: F401 -- registra tutti i modelli in Base
    from app.config import settings
    from app.database import Base
    from app.utils.db_dialect import to_async_database_url

    eng = create_async_engine(to_async_database_url(settings.database_url))
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await eng.dispose()


def esito(nome: str, ok: bool, dettaglio: str = "") -> None:
    stato = "PASS" if ok else "FAIL"
    print(f"\n=== {stato} -- {nome} ===")
    if dettaglio:
        print(dettaglio)
    if not ok:
        raise SystemExit(1)
