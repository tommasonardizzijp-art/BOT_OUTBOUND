import asyncio

import pytest

from app.services import wa_discover_runs
from tests.factories_wa import make_discover_run, make_number, make_tenant


def test_copertura_include_i_salti():
    # Senza i salti una riscansione riuscita sembrerebbe una raccolta al 2%.
    esito = {"salvate": 3, "aggiornate": 2, "saltate_gia_note": 90,
             "non_verificate": 0, "dichiarato": 100}
    assert wa_discover_runs.calcola_copertura(esito) == 95


def test_copertura_none_se_il_dichiarato_manca():
    esito = {"salvate": 10, "aggiornate": 0, "saltate_gia_note": 0,
             "non_verificate": 0, "dichiarato": None}
    assert wa_discover_runs.calcola_copertura(esito) is None


def test_copertura_none_se_il_dichiarato_e_zero():
    esito = {"salvate": 0, "aggiornate": 0, "saltate_gia_note": 0,
             "non_verificate": 0, "dichiarato": 0}
    assert wa_discover_runs.calcola_copertura(esito) is None


def test_copertura_non_supera_cento():
    # Il dichiarato di WhatsApp non e' affidabile al singolo: una raccolta
    # superiore non deve produrre "137%" in UI.
    esito = {"salvate": 137, "aggiornate": 0, "saltate_gia_note": 0,
             "non_verificate": 0, "dichiarato": 100}
    assert wa_discover_runs.calcola_copertura(esito) == 100


@pytest.mark.asyncio
async def test_apri_run_la_rende_visibile_come_attiva(db_session):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)

    run = await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id,
                                          number_id=number.id)
    await db_session.commit()

    attiva = await wa_discover_runs.run_attiva(db_session, number.id)
    assert attiva is not None and attiva.id == run.id


@pytest.mark.asyncio
async def test_chiudi_run_scrive_contatori_stato_e_copertura(db_session):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    run = await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id,
                                          number_id=number.id)
    await db_session.commit()

    await wa_discover_runs.chiudi_run(db_session, run.id, {
        "salvate": 60, "aggiornate": 5, "saltate_gia_note": 20,
        "non_verificate": 2, "dichiarato": 100, "motivo": "completato",
        "sync_letta": None, "sync_stato": "assente",
    })
    await db_session.commit()

    assert await wa_discover_runs.run_attiva(db_session, number.id) is None
    chiusa = await wa_discover_runs.ultima_run(db_session, number.id)
    assert chiusa.stato == "done"
    assert chiusa.finished_at is not None
    assert (chiusa.salvate, chiusa.aggiornate, chiusa.saltate_gia_note) == (60, 5, 20)
    assert chiusa.copertura == 85
    assert chiusa.motivo == "completato"
    assert chiusa.sync_stato == "assente"


@pytest.mark.asyncio
async def test_chiudi_run_con_errore_va_in_failed(db_session):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    run = await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id,
                                          number_id=number.id)
    await db_session.commit()

    await wa_discover_runs.chiudi_run(db_session, run.id, {},
                                      errore="RuntimeError: browser sparito")
    await db_session.commit()

    chiusa = await wa_discover_runs.ultima_run(db_session, number.id)
    assert chiusa.stato == "failed"
    assert chiusa.motivo == "errore_imprevisto"
    assert "browser sparito" in chiusa.errore


@pytest.mark.asyncio
async def test_chiudi_run_con_errore_maschera_un_numero_in_chiaro(db_session):
    # P12: nessun numero di telefono in chiaro in una colonna testuale.
    # Oggi il motore non ri-solleva mai (blanket except in
    # wa_discover_run.py), ma 'titolo_atteso' dentro il pannello e' spesso il
    # numero grezzo -- chiudi_run e' l'ultimo cancello prima del DB e non
    # deve fidarsi che nessun raise futuro lo porti in un'eccezione.
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    run = await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id,
                                          number_id=number.id)
    await db_session.commit()

    await wa_discover_runs.chiudi_run(
        db_session, run.id, {},
        errore="RuntimeError: titolo atteso +39 333 1234567 non trovato")
    await db_session.commit()

    chiusa = await wa_discover_runs.ultima_run(db_session, number.id)
    assert "333" not in chiusa.errore and "1234567" not in chiusa.errore
    assert "<num>" in chiusa.errore
    assert "titolo atteso" in chiusa.errore


@pytest.mark.asyncio
async def test_chiudi_run_con_motivo_non_mappato_va_in_failed(db_session):
    # Se il motore aggiunge domani un motivo nuovo e nessuno lo mette in
    # MOTIVI_NON_GUASTO, la run deve risultare failed -- altrimenti
    # l'informazione di un guasto vero sparisce esattamente quando serve,
    # cioe' il contrario del motivo per cui questa tabella esiste.
    #
    # Prova del nove eseguita a mano durante questo fix: sostituendo
    # `"done" if motivo in MOTIVI_NON_GUASTO else "failed"` con `"done"`
    # fisso in chiudi_run, SOLO questo test diventa rosso (tutti gli altri
    # 9 di questo file restano verdi, perche' usano tutti motivi mappati).
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    run = await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id,
                                          number_id=number.id)
    await db_session.commit()

    await wa_discover_runs.chiudi_run(db_session, run.id,
                                      {"motivo": "qualcosa_di_non_mappato"})
    await db_session.commit()

    chiusa = await wa_discover_runs.ultima_run(db_session, number.id)
    assert chiusa.stato == "failed"
    assert chiusa.motivo == "qualcosa_di_non_mappato"


@pytest.mark.asyncio
async def test_chiudi_run_maschera_separatori_ripetuti_e_no_break_space(db_session):
    # [\s.\-/]? (un solo separatore) lasciava scoperto il pezzo di cifre
    # prima di un secondo separatore consecutivo -- doppio spazio, o il
    # doppio no-break space visto in un titolo WhatsApp vero
    # ('Sofia\xa0\xa0D'ari'). Verificato prima del fix:
    # "333\xa0\xa01234567" -> "333\xa0\xa0<num>" (333 in chiaro).
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    run = await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id,
                                          number_id=number.id)
    await db_session.commit()

    await wa_discover_runs.chiudi_run(
        db_session, run.id, {},
        errore=("RuntimeError: titolo atteso 333\xa0\xa01234567 e poi "
                "333  1234567 non trovato"))
    await db_session.commit()

    chiusa = await wa_discover_runs.ultima_run(db_session, number.id)
    assert "333" not in chiusa.errore
    assert "1234567" not in chiusa.errore
    assert chiusa.errore.count("<num>") == 2


@pytest.mark.asyncio
async def test_chiudi_run_concorrente_non_produce_un_ibrido(db_session):
    """Il worker (Task 4, a fine scan) e l'endpoint (Task 5, quando
    l'accodamento ARQ fallisce) possono chiudere la STESSA run quasi in
    contemporanea. Riprodotto in review con due chiudi_run in
    asyncio.gather su due sessioni DB indipendenti: senza CAS la riga finale
    era un ibrido (stato='failed' coi contatori della run riuscita).

    L'asserzione e' l'INVARIANTE, non quale delle due chiamate vince (SQLite
    serializza i commit quindi l'esito e' deterministico a runtime, ma non e'
    il comportamento che questo test protegge -- asserire il ramo invece
    dell'invariante e' un errore gia' fatto su questo progetto): la riga
    finale deve essere internamente coerente, o 'done' con i contatori
    dell'esito ed errore nullo, o 'failed' con l'errore e i contatori a
    zero, mai un misto delle due chiamate.

    La lettura finale usa una sessione NUOVA, mai db_session: db_session ha
    gia' in identity map l'oggetto WaDiscoverRun creato da apri_run (stessa
    PK), e un UPDATE Core fatto da UN'ALTRA sessione non la invalida --
    db_session tornerebbe l'oggetto stantio (ancora 'running') anche a
    scrittura sul disco gia' avvenuta e committata da entrambe le sessioni
    concorrenti. Verificato in debug: due letture indipendenti concordavano
    sullo stato vero (es. 'failed'), la terza via db_session diceva ancora
    'running'. E' un artefatto dell'identity map della sessione che ha
    creato la run, non un difetto di chiudi_run -- lo stesso motivo per cui
    in produzione il worker (Task 4) usa una AsyncSessionLocal() propria e
    mai la sessione della richiesta HTTP che ha aperto la run.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import settings
    from app.utils.db_dialect import to_async_database_url

    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    run = await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id,
                                          number_id=number.id)
    await db_session.commit()

    eng = create_async_engine(to_async_database_url(settings.database_url))
    maker = async_sessionmaker(eng, expire_on_commit=False)

    async def _chiude_con_successo():
        async with maker() as s:
            await wa_discover_runs.chiudi_run(s, run.id, {
                "salvate": 78, "aggiornate": 0, "saltate_gia_note": 0,
                "non_verificate": 0, "dichiarato": 80, "motivo": "completato",
            })
            await s.commit()

    async def _chiude_con_errore():
        async with maker() as s:
            await wa_discover_runs.chiudi_run(s, run.id, {},
                                              errore="orfana oltre il TTL")
            await s.commit()

    try:
        await asyncio.gather(_chiude_con_successo(), _chiude_con_errore())

        async with maker() as s3:
            chiusa = await wa_discover_runs.ultima_run(s3, number.id)
    finally:
        await eng.dispose()

    if chiusa.stato == "done":
        assert chiusa.motivo == "completato"
        assert chiusa.salvate == 78
        assert chiusa.errore is None
    else:
        assert chiusa.stato == "failed"
        assert chiusa.motivo == "errore_imprevisto"
        assert chiusa.errore is not None
        assert chiusa.salvate == 0


@pytest.mark.asyncio
async def test_chiudi_run_gia_chiusa_non_la_riapre(db_session):
    # Il worker puo' chiamare chiudi_run due volte (esito + finally di guardia):
    # la seconda non deve sovrascrivere finished_at ne' i contatori.
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    run = await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id,
                                          number_id=number.id)
    await db_session.commit()
    await wa_discover_runs.chiudi_run(db_session, run.id,
                                      {"salvate": 7, "motivo": "completato"})
    await db_session.commit()
    primo_finished = (await wa_discover_runs.ultima_run(db_session, number.id)).finished_at

    await wa_discover_runs.chiudi_run(db_session, run.id, {}, errore="tardiva")
    await db_session.commit()

    chiusa = await wa_discover_runs.ultima_run(db_session, number.id)
    assert chiusa.stato == "done"
    assert chiusa.salvate == 7
    assert chiusa.finished_at == primo_finished


@pytest.mark.asyncio
async def test_storico_torna_le_run_dalla_piu_recente(db_session):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    vecchia = await make_discover_run(db_session, tenant, number, stato="done",
                                      motivo="completato")
    recente = await make_discover_run(db_session, tenant, number, stato="done",
                                      motivo="raccolta_parziale")
    await db_session.commit()
    # started_at ha default a livello Python: due righe create nello stesso
    # microsecondo romperebbero l'ordinamento. Le si separa esplicitamente.
    vecchia.started_at = vecchia.started_at.replace(year=2020)
    await db_session.commit()

    righe = await wa_discover_runs.storico(db_session, number.id, limit=10)
    assert [r.id for r in righe] == [recente.id, vecchia.id]
