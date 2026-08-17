"""Adversarial A.10 -- run lasciata 'running' a mano oltre il TTL, poi un
POST reale sullo stesso numero.

docs/superpowers/qa/2026-08-15-qa-wa-discover-lancio-adversarial.md, caso 10:
"Run lasciata 'running' a mano oltre il TTL (UPDATE diretto a DB su
started_at), poi un POST sullo stesso numero -> il gate del Task 10 la
chiude DA SOLO e sblocca il numero (non 409 scan_gia_in_corso sulla run
morta); verificare A DB con una sessione fresca, non quella della richiesta,
che la vecchia riga sia davvero failed/run_orfana e non ancora running."

Differenza rispetto ai test unit gia' in test_wa_discover_run_orfana.py
(che chiamano chiudi_se_orfana/puo_lanciare direttamente): qui si passa
dall'endpoint HTTP vero (POST /api/wa/numbers/{id}/discover), end-to-end,
esattamente come lo chiamerebbe il frontend.

Uso:
    cd "D:\\BOT OUTBOUND\\backend"
    .\\venv\\Scripts\\python.exe scripts/qa_wa_discover/adv_a10_orfana_poi_post_reale.py
"""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

import _bootstrap  # noqa: E402

from app.utils.tempo import adesso_utc  # noqa: E402


@contextmanager
def _override_dipendenze(app, get_db, get_current_user, session_maker):
    from app.models.user import User

    def _admin() -> User:
        return User(id="00000000-0000-0000-0000-0000000000a1",
                   email="admin-adv-a10@test.local", password_hash="x",
                   role="admin", is_active=True, created_at=datetime(2026, 1, 1))

    async def _get_db():
        async with session_maker() as s:
            yield s

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = _admin
    try:
        yield
    finally:
        app.dependency_overrides.clear()


async def main() -> None:
    await _bootstrap.crea_schema_pulito()

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.api import wa_numbers
    from app.config import settings
    from app.database import get_db
    from app.main import app
    from app.models.wa import WaDiscoverRun
    from app.utils.auth_deps import get_current_user
    from app.utils.db_dialect import to_async_database_url
    from tests.factories_wa import make_number, make_tenant

    eng = create_async_engine(to_async_database_url(settings.database_url))
    maker = async_sessionmaker(eng, expire_on_commit=False)

    async with maker() as setup_db:
        tenant = await make_tenant(setup_db)
        number = await make_number(setup_db, tenant)
        from app.services import wa_discover_runs
        vecchia = await wa_discover_runs.apri_run(
            setup_db, tenant_id=tenant.id, number_id=number.id)
        # Oltre la soglia orfana, scritto A MANO (UPDATE diretto), non via
        # chiudi_se_orfana: e' esattamente lo scenario del caso 10.
        vecchia.started_at = (adesso_utc()
                              - timedelta(minutes=settings.wa_discover_run_orfana_min + 5))
        await setup_db.commit()
        vecchia_id = vecchia.id
        number_id = number.id

    # Solo le guardie che dipendono da servizi esterni (Redis/RAM) sono
    # mockate al verde: chiudi_se_orfana, run_attiva, apri_run restano
    # QUELLI VERI -- e' la parte sotto esame.
    async def _redis_libero(*a, **kw):
        return None

    async def _kill_switch_spento(*a, **kw):
        return False

    async def _enqueue_ok(number_id, run_id):
        return True

    with patch.object(wa_numbers.wa_discover_gate.bot_state_service,
                      "is_wa_halted", _kill_switch_spento), \
         patch.object(wa_numbers.wa_discover_gate.wa_profile_lock,
                      "profilo_occupato_da", _redis_libero), \
         patch.object(wa_numbers.wa_discover_gate, "ram_libera_mb", lambda: 4000), \
         patch.object(wa_numbers, "enqueue_wa_discover", _enqueue_ok), \
         _override_dipendenze(app, get_db, get_current_user, maker):
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            r = await c.post(f"/api/wa/numbers/{number_id}/discover")

    problemi = []
    if r.status_code != 200:
        problemi.append(f"POST atteso 200 (sblocco dopo auto-guarigione), "
                        f"ricevuto {r.status_code}: {r.text}")
    else:
        nuovo_run_id = r.json().get("run_id")
        if not nuovo_run_id or nuovo_run_id == vecchia_id:
            problemi.append(f"run_id nuovo mancante o uguale alla vecchia: {r.json()}")

    # Sessione FRESCA, indipendente da quella della richiesta: prova vera
    # che la guarigione e' a DB, non solo nella transazione del chiamante
    # (stesso principio di test_orfana_chiusa_sopravvive_anche_se_il_gate_rifiuta_dopo).
    eng2 = create_async_engine(to_async_database_url(settings.database_url))
    maker2 = async_sessionmaker(eng2, expire_on_commit=False)
    async with maker2() as fresca:
        vecchia_riletta = await fresca.scalar(
            select(WaDiscoverRun).where(WaDiscoverRun.id == vecchia_id))
        if vecchia_riletta is None:
            problemi.append("la vecchia run e' sparita dal DB")
        elif vecchia_riletta.stato != "failed":
            problemi.append(
                f"la vecchia run doveva essere 'failed' (chiusa come orfana), "
                f"trovata stato={vecchia_riletta.stato!r}")
        elif vecchia_riletta.motivo != "run_orfana":
            problemi.append(
                f"la vecchia run e' 'failed' ma motivo={vecchia_riletta.motivo!r}, "
                "atteso 'run_orfana'")

        righe_running = (await fresca.execute(
            select(WaDiscoverRun).where(
                WaDiscoverRun.number_id == number_id,
                WaDiscoverRun.stato == "running"))).scalars().all()
        if len(righe_running) != 1:
            problemi.append(
                f"attesa esattamente 1 riga 'running' (la nuova run aperta dal "
                f"POST), trovate {len(righe_running)}")
    await eng2.dispose()
    await eng.dispose()

    if problemi:
        _bootstrap.esito("A.10 run orfana poi POST reale", False, "\n".join(problemi))
    else:
        _bootstrap.esito(
            "A.10 run orfana poi POST reale", True,
            f"POST reale su un numero con una run 'running' lasciata a mano oltre "
            f"la soglia orfana ({settings.wa_discover_run_orfana_min} min) ha "
            "risposto 200 (non 409 scan_gia_in_corso). Sessione fresca conferma: "
            "vecchia run failed/run_orfana, esattamente 1 riga running (la nuova).")


asyncio.run(main())
