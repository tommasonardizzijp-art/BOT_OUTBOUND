from datetime import datetime, timedelta, timezone

import pytest

from app.services import wa_discover_gate, wa_discover_runs
from app.utils.tempo import adesso_utc
from tests.factories_wa import make_discover_run, make_number, make_tenant


# --------------------------------------------------------------------------
# started_at TZ-AWARE: il caso che i test non vedevano e la produzione si'.
#
# La colonna e' DateTime(timezone=True) -> su PostgreSQL e' timestamptz e
# SQLAlchemy restituisce un datetime AWARE; su SQLite (dove gira questa suite,
# vedi conftest) torna NAIVE. Il confronto in chiudi_se_orfana avveniva contro
# datetime.utcnow(), che e' naive: verde qui, TypeError in produzione. Il
# TypeError risaliva fino all'endpoint, che rispondeva 500 al posto del 409
# "scan_gia_in_corso" -- cioe' proprio il rifiuto leggibile che il gate esiste
# per dare. Trovato dal vivo il 16/08, dopo il merge della PR #85.
#
# Questi due test fabbricano la run con un istante AWARE a prescindere dal
# dialetto sotto: e' l'unico modo di pinnare il caso in una suite su SQLite.
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_started_at_di_default_e_aware(db_session):
    """Il default della colonna deve produrre un istante AWARE.

    apri_run non passa started_at: lo mette il default del modello. Un naive
    scritto su timestamptz viene letto come ora locale e finisce a DB
    spostato di tutto l'offset del fuso (2 h in ora legale su questa
    macchina), il che erode il margine fra wa_discover_run_orfana_min e
    wa_discover_job_timeout_s fino a farlo diventare negativo.
    """
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    run = await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id,
                                          number_id=number.id)

    assert run.started_at.tzinfo is not None


@pytest.mark.asyncio
async def test_run_recente_aware_non_esplode(db_session, monkeypatch):
    """Run RECENTE con started_at aware: si rifiuta, non si solleva."""
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    run = await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id,
                                          number_id=number.id)
    run.started_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db_session.commit()

    async def _run_attiva_aware(db, number_id):
        return run

    monkeypatch.setattr(wa_discover_runs, "run_attiva", _run_attiva_aware)

    assert await wa_discover_runs.chiudi_se_orfana(db_session, number.id) is False


@pytest.mark.asyncio
async def test_run_vecchia_aware_viene_chiusa(db_session, monkeypatch):
    """Run VECCHIA con started_at aware: l'auto-guarigione deve scattare."""
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    run = await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id,
                                          number_id=number.id)
    run.started_at = datetime.now(timezone.utc) - timedelta(hours=9)
    await db_session.commit()

    async def _run_attiva_aware(db, number_id):
        return run

    monkeypatch.setattr(wa_discover_runs, "run_attiva", _run_attiva_aware)

    assert await wa_discover_runs.chiudi_se_orfana(db_session, number.id) is True


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
    run.started_at = adesso_utc() - timedelta(hours=9)
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
    # Neutralizza anche il gate sul commit: senza, questi test dipenderebbero
    # dalla memoria reale della macchina. `raising=False` perche' quella funzione
    # arriva con un'altra PR: cosi' questo file sta in piedi con e senza, invece
    # di legare l'ordine dei merge.
    monkeypatch.setattr(wa_discover_gate, "commit_disponibile_mb", lambda: 20000,
                        raising=False)

    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    run = await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id,
                                          number_id=number.id)
    run.started_at = adesso_utc() - timedelta(hours=9)
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
    # Neutralizza anche il gate sul commit: senza, questi test dipenderebbero
    # dalla memoria reale della macchina. `raising=False` perche' quella funzione
    # arriva con un'altra PR: cosi' questo file sta in piedi con e senza, invece
    # di legare l'ordine dei merge.
    monkeypatch.setattr(wa_discover_gate, "commit_disponibile_mb", lambda: 20000,
                        raising=False)

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
    run.started_at = adesso_utc() - timedelta(hours=9)
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


@pytest.mark.asyncio
async def test_orfana_non_sovrascrive_una_chiusura_legittima_nel_mezzo(db_session, monkeypatch):
    """Riprodotto dal reviewer: un worker che NON era morto -- stava solo
    per finire -- chiude la run con successo vero (done/completato/
    salvate=500) nella finestra fra la lettura di chiudi_se_orfana e la sua
    scrittura. Il difetto vecchio: due scritture separate (chiudi_run(errore)
    poi un SELECT + `motivo = 'run_orfana'` incondizionato) che non
    controllava se la prima avesse davvero trovato la riga 'running'. La
    riga finale mescolava stato='done'/salvate=500 col motivo 'run_orfana'
    -- falso, ed esattamente nel campo che diciamo essere "il primo posto
    dove si guarda per capire cosa e' successo senza aprire i log". Era gia'
    nella lista adversarial (#3), nessuno l'aveva eseguito.

    L'invariante, non il ramo: 'done' deve implicare motivo=='completato' e
    i contatori veri, mai un ibrido.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import settings
    from app.utils.db_dialect import to_async_database_url

    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    run = await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id,
                                          number_id=number.id)
    run.started_at = adesso_utc() - timedelta(hours=9)
    await db_session.commit()

    chiudi_run_vero = wa_discover_runs.chiudi_run
    eng = create_async_engine(to_async_database_url(settings.database_url))
    maker = async_sessionmaker(eng, expire_on_commit=False)

    async def chiudi_run_con_interferenza(*a, **kw):
        # "Nel mezzo": chiudi_se_orfana ha gia' deciso (run letta 'running',
        # oltre soglia) e sta per chiamare chiudi_run -- PRIMA che lo faccia,
        # un'altra sessione chiude la run per davvero, con successo vero.
        async with maker() as s_legittima:
            await chiudi_run_vero(s_legittima, run.id, {
                "salvate": 500, "aggiornate": 0, "saltate_gia_note": 0,
                "non_verificate": 0, "dichiarato": 500, "motivo": "completato",
            })
            await s_legittima.commit()
        return await chiudi_run_vero(*a, **kw)

    monkeypatch.setattr(wa_discover_runs, "chiudi_run", chiudi_run_con_interferenza)

    try:
        await wa_discover_runs.chiudi_se_orfana(db_session, number.id)
    finally:
        await eng.dispose()

    riga = await wa_discover_runs.ultima_run(db_session, number.id)
    if riga.stato == "done":
        assert riga.motivo == "completato"
        assert riga.salvate == 500
    else:
        # Non dovrebbe capitare (l'interferenza chiude PRIMA della scrittura
        # di chiudi_se_orfana), ma se capitasse comunque MAI un ibrido:
        # 'run_orfana' coi contatori di una raccolta vera.
        assert riga.stato == "failed"
        assert riga.salvate == 0
