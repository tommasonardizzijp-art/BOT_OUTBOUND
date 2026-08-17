"""Adversarial H -- volumi (docs/superpowers/qa/2026-08-15-qa-wa-discover-lancio-adversarial.md, casi 41, 44 -- solo meta' backend).

Casi 42, 43 e la meta' UI del 44 NON qui: richiedono un frontend vero in
esecuzione (SWR, rendering, refresh automatico) -- nessun browser
disponibile ora (scan WhatsApp live in corso). SKIP dichiarato, non
sostituito con un equivalente piu' debole.

Uso:
    cd "D:\\BOT OUTBOUND\\backend"
    .\\venv\\Scripts\\python.exe scripts/qa_wa_discover/adv_h_volumi.py
"""
import asyncio
from datetime import datetime

import _bootstrap  # noqa: E402


async def _caso41_storico_rispetta_il_limite(app, maker) -> tuple[bool, str]:
    """41. Storico con 50+ run sullo stesso numero -> GET rispetta
    wa_discover_storico_limit (default 10), non torna tutto."""
    from httpx import ASGITransport, AsyncClient

    from app.config import settings
    from app.database import get_db
    from app.models.user import User
    from app.utils.auth_deps import get_current_user
    from tests.factories_wa import make_discover_run, make_number, make_tenant

    async with maker() as db:
        tenant = await make_tenant(db)
        number = await make_number(db, tenant)
        for i in range(55):
            await make_discover_run(db, tenant, number, stato="done", salvate=i)
        await db.commit()
        number_id = number.id

    def _admin() -> User:
        return User(id="00000000-0000-0000-0000-0000000000h1",
                   email="admin-adv-h41@test.local", password_hash="x",
                   role="admin", is_active=True, created_at=datetime(2026, 1, 1))

    async def _get_db():
        async with maker() as s:
            yield s

    from app.main import app as real_app
    real_app.dependency_overrides[get_db] = _get_db
    real_app.dependency_overrides[get_current_user] = _admin
    try:
        async with AsyncClient(transport=ASGITransport(app=real_app),
                               base_url="http://test") as c:
            r = await c.get(f"/api/wa/numbers/{number_id}/discover")
    finally:
        real_app.dependency_overrides.clear()

    if r.status_code != 200:
        return False, f"GET con 55 run pregresse ha risposto {r.status_code}"
    n = len(r.json()["storico"])
    if n != settings.wa_discover_storico_limit:
        return False, (f"storico atteso {settings.wa_discover_storico_limit} righe "
                       f"(wa_discover_storico_limit), ricevute {n} (55 scritte a DB)")
    return True, (f"55 run scritte a DB, GET ne torna {n} == "
                 f"wa_discover_storico_limit ({settings.wa_discover_storico_limit})")


async def _caso44_troncamento_backend(maker) -> tuple[bool, str]:
    """44 (solo meta' backend -- la UI che "non rompe il layout" e' fuori
    portata senza browser). Motivo/errore lunghissimo (oltre 2000
    caratteri) -> chiudi_run tronca, nessun crash, nessuna scrittura
    illimitata a DB."""
    from sqlalchemy import select

    from app.models.wa import WaDiscoverRun
    from app.services import wa_discover_runs
    from tests.factories_wa import make_number, make_tenant

    errore_lunghissimo = "elemento non trovato: " + ("x" * 5000)
    async with maker() as db:
        tenant = await make_tenant(db)
        number = await make_number(db, tenant)
        run = await wa_discover_runs.apri_run(db, tenant_id=tenant.id, number_id=number.id)
        await db.commit()
        run_id = run.id

    async with maker() as db:
        try:
            await wa_discover_runs.chiudi_run(db, run_id, {}, errore=errore_lunghissimo)
            await db.commit()
        except Exception as exc:  # noqa: BLE001
            return False, f"chiudi_run con un errore di 5023 caratteri ha sollevato ({type(exc).__name__}: {exc})"

    async with maker() as db:
        chiusa = await db.scalar(select(WaDiscoverRun).where(WaDiscoverRun.id == run_id))
    lunghezza = len(chiusa.errore or "")
    if lunghezza > 2000:
        return False, f"errore NON troncato: {lunghezza} caratteri a DB (atteso <= 2000)"
    return True, f"errore di 5023 caratteri in input -> {lunghezza} caratteri a DB (troncato, nessun crash)"


async def main() -> None:
    await _bootstrap.crea_schema_pulito()

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import settings
    from app.main import app
    from app.utils.db_dialect import to_async_database_url

    eng = create_async_engine(to_async_database_url(settings.database_url))
    maker = async_sessionmaker(eng, expire_on_commit=False)

    esiti = {}
    esiti["H.41"] = await _caso41_storico_rispetta_il_limite(app, maker)
    esiti["H.44 (backend)"] = await _caso44_troncamento_backend(maker)

    await eng.dispose()

    tutti_ok = True
    for nome, (ok, dettaglio) in esiti.items():
        print(f"\n=== {'PASS' if ok else 'FAIL'} -- {nome} ===\n{dettaglio}")
        tutti_ok = tutti_ok and ok

    if not tutti_ok:
        raise SystemExit(1)


asyncio.run(main())
