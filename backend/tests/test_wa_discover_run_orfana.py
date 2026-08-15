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


@pytest.mark.asyncio
async def test_orfana_chiusa_sopravvive_anche_se_il_gate_rifiuta_dopo(db_session, monkeypatch):
    # Lo scenario esatto trovato in review: chiudi_se_orfana chiude l'orfana,
    # ma una guardia SUCCESSIVA (qui: RAM) rifiuta comunque. puo_lanciare non
    # committa mai di suo -- se la chiusura vivesse solo sulla sessione del
    # chiamante, l'HTTPException del 409 la perderebbe col rollback implicito
    # di get_db(). Con la sessione propria di chiudi_se_orfana la chiusura
    # deve sopravvivere A PRESCINDERE da cosa fa il chiamante dopo.
    async def _async_none(*a, **kw):
        return None

    async def _async_false(*a, **kw):
        return False

    monkeypatch.setattr(wa_discover_gate.bot_state_service, "is_wa_halted", _async_false)
    monkeypatch.setattr(wa_discover_gate.wa_profile_lock, "profilo_occupato_da", _async_none)
    # RAM insufficiente: il gate rifiuta DOPO aver chiuso l'orfana (l'ordine
    # nel gate e' chiudi_se_orfana -> scan_gia_in_corso -> ram_insufficiente).
    monkeypatch.setattr(wa_discover_gate, "ram_libera_mb", lambda: 300)

    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    run = await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id,
                                          number_id=number.id)
    run.started_at = datetime.utcnow() - timedelta(hours=9)
    await db_session.commit()

    codice = await wa_discover_gate.puo_lanciare(db_session, number)
    assert codice == "ram_insufficiente"

    # NON si committa db_session apposta: e' esattamente cio' che succede
    # nell'endpoint reale quando puo_lanciare rifiuta (HTTPException, nessun
    # commit, get_db() chiude la sessione con un rollback implicito). Una
    # sessione FRESCA e indipendente, mai toccata da questo test, prova che
    # la chiusura e' davvero a DB e non solo nella transazione del chiamante.
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import settings
    from app.utils.db_dialect import to_async_database_url

    eng = create_async_engine(to_async_database_url(settings.database_url))
    maker = async_sessionmaker(eng, expire_on_commit=False)
    try:
        async with maker() as s:
            assert await wa_discover_runs.run_attiva(s, number.id) is None
            chiusa = await wa_discover_runs.ultima_run(s, number.id)
            assert chiusa.stato == "failed"
            assert chiusa.motivo == "run_orfana"
    finally:
        await eng.dispose()
