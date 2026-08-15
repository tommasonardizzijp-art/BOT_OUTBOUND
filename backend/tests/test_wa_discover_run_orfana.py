from datetime import datetime, timedelta

import pytest

from app.services import wa_discover_gate, wa_discover_runs
from tests.factories_wa import make_discover_run, make_number, make_tenant


@pytest.mark.asyncio
async def test_run_recente_non_viene_chiusa(db_session):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id, number_id=number.id)
    await db_session.commit()

    assert await wa_discover_runs.chiudi_se_orfana(db_session, number.id) is False
    assert await wa_discover_runs.run_attiva(db_session, number.id) is not None


@pytest.mark.asyncio
async def test_run_vecchia_viene_chiusa_come_orfana(db_session):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    run = await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id,
                                          number_id=number.id)
    run.started_at = datetime.utcnow() - timedelta(hours=9)
    await db_session.commit()

    assert await wa_discover_runs.chiudi_se_orfana(db_session, number.id) is True
    await db_session.commit()

    assert await wa_discover_runs.run_attiva(db_session, number.id) is None
    chiusa = await wa_discover_runs.ultima_run(db_session, number.id)
    assert chiusa.stato == "failed"
    assert chiusa.motivo == "run_orfana"


@pytest.mark.asyncio
async def test_senza_nessuna_run_non_fa_niente(db_session):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await db_session.commit()

    assert await wa_discover_runs.chiudi_se_orfana(db_session, number.id) is False


@pytest.mark.asyncio
async def test_il_gate_sblocca_il_numero_dopo_aver_chiuso_l_orfana(db_session, monkeypatch):
    # L'invariante che conta: un worker morto NON deve rendere il numero
    # non piu' scansionabile per sempre.
    async def _async_none(*a, **kw):
        return None

    async def _async_false(*a, **kw):
        return False

    monkeypatch.setattr(wa_discover_gate.bot_state_service, "is_wa_halted", _async_false)
    monkeypatch.setattr(wa_discover_gate.wa_profile_lock, "profilo_occupato_da", _async_none)
    monkeypatch.setattr(wa_discover_gate, "ram_libera_mb", lambda: 4000)

    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    run = await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id,
                                          number_id=number.id)
    run.started_at = datetime.utcnow() - timedelta(hours=9)
    await db_session.commit()

    assert await wa_discover_gate.puo_lanciare(db_session, number) is None


@pytest.mark.asyncio
async def test_il_gate_rifiuta_ancora_se_la_run_e_recente(db_session, monkeypatch):
    async def _async_none(*a, **kw):
        return None

    async def _async_false(*a, **kw):
        return False

    monkeypatch.setattr(wa_discover_gate.bot_state_service, "is_wa_halted", _async_false)
    monkeypatch.setattr(wa_discover_gate.wa_profile_lock, "profilo_occupato_da", _async_none)
    monkeypatch.setattr(wa_discover_gate, "ram_libera_mb", lambda: 4000)

    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id, number_id=number.id)
    await db_session.commit()

    assert await wa_discover_gate.puo_lanciare(db_session, number) == "scan_gia_in_corso"
