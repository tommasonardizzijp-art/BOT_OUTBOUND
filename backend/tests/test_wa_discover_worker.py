import asyncio

import pytest

from app.services import wa_discover_runs
from app.workers import wa_discover_worker
from tests.factories_wa import make_number, make_tenant


def test_il_job_id_contiene_il_run_id_non_il_number_id():
    # enqueue_wa_workers usa wa:send:{number_id} deterministico, e ARQ scarta
    # in silenzio il duplicato: accodati 0, nessun errore. Legando l'id alla
    # run, ogni scansione e' un job distinto e quel guasto muto non si ripete.
    assert wa_discover_worker.wa_discover_job_id("run-abc") == "wa:discover:run-abc"
    assert "run-abc" in wa_discover_worker.wa_discover_job_id("run-abc")


@pytest.mark.asyncio
async def test_il_task_chiude_la_run_con_l_esito_del_motore(db_session, monkeypatch):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    run = await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id,
                                          number_id=number.id)
    await db_session.commit()

    async def finto_motore(number_id, **kw):
        return {"salvate": 12, "aggiornate": 1, "saltate_gia_note": 40,
                "non_verificate": 0, "dichiarato": 60, "motivo": "completato"}

    monkeypatch.setattr(wa_discover_worker, "esegui_discover_run", finto_motore)
    monkeypatch.setattr(wa_discover_worker, "AsyncSessionLocal",
                        _sessione_finta(db_session))

    await wa_discover_worker.wa_discover_task({}, number.id, run.id)

    chiusa = await wa_discover_runs.ultima_run(db_session, number.id)
    assert chiusa.stato == "done"
    assert chiusa.salvate == 12
    assert chiusa.copertura == 88


@pytest.mark.asyncio
async def test_se_il_motore_solleva_la_run_finisce_in_failed(db_session, monkeypatch):
    # esegui_discover_run oggi non solleva mai, ma la run non deve restare
    # 'running' per sempre se un giorno lo facesse: una run appesa blocca
    # ogni scansione futura su quel numero (unique parziale).
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    run = await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id,
                                          number_id=number.id)
    await db_session.commit()

    async def motore_che_esplode(number_id, **kw):
        raise RuntimeError("browser sparito")

    monkeypatch.setattr(wa_discover_worker, "esegui_discover_run", motore_che_esplode)
    monkeypatch.setattr(wa_discover_worker, "AsyncSessionLocal",
                        _sessione_finta(db_session))

    await wa_discover_worker.wa_discover_task({}, number.id, run.id)

    chiusa = await wa_discover_runs.ultima_run(db_session, number.id)
    assert chiusa.stato == "failed"
    assert "browser sparito" in chiusa.errore


@pytest.mark.asyncio
async def test_se_il_motore_viene_cancellato_la_run_si_chiude_e_l_eccezione_risale(
        db_session, monkeypatch):
    # ARQ cancella il job al timeout (asyncio.wait_for): CancelledError
    # eredita da BaseException, non da Exception -- il try/except Exception
    # normale non la vede. Senza gestione dedicata la run resta 'running'
    # per sempre (indice unico parziale, numero non piu' scansionabile).
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    run = await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id,
                                          number_id=number.id)
    await db_session.commit()

    async def motore_cancellato(number_id, **kw):
        raise asyncio.CancelledError()

    monkeypatch.setattr(wa_discover_worker, "esegui_discover_run", motore_cancellato)
    monkeypatch.setattr(wa_discover_worker, "AsyncSessionLocal",
                        _sessione_finta(db_session))

    # ARQ deve poter vedere la cancellazione: se wa_discover_task la
    # ingoiasse, ARQ non saprebbe che il job e' morto invece che completato.
    with pytest.raises(asyncio.CancelledError):
        await wa_discover_worker.wa_discover_task({}, number.id, run.id)

    chiusa = await wa_discover_runs.ultima_run(db_session, number.id)
    assert chiusa.stato == "failed"
    assert chiusa.motivo == "cancellato"


def test_il_task_e_registrato_nel_worker():
    # Un job non registrato viene accodato e non parte mai: la run resta
    # 'running' e il numero non e' piu' scansionabile.
    from app.workers.task_queue import WorkerSettings

    nomi = {getattr(f, "__name__", getattr(f, "coroutine", None) and f.coroutine.__name__)
            for f in WorkerSettings.functions}
    assert "wa_discover_task" in nomi


@pytest.mark.asyncio
async def test_se_chiudi_run_solleva_il_task_non_propaga(db_session, monkeypatch):
    # Review: il try originale copriva solo esegui_discover_run, non la
    # chiusura/commit. Un blip del DB proprio li' (scenario reale: un
    # TimeoutError di asyncpg sul pooler Supabase e' gia' successo) faceva
    # risalire l'eccezione, lasciando la run 'running' per sempre (indice
    # unico parziale, numero mai piu' scansionabile). Qui chiudi_run
    # fallisce SEMPRE (anche il secondo tentativo "a mani nude"): il task
    # non deve comunque sollevare.
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    run = await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id,
                                          number_id=number.id)
    await db_session.commit()

    async def finto_motore(number_id, **kw):
        return {"salvate": 1, "motivo": "completato"}

    async def chiudi_run_che_esplode(*a, **kw):
        raise TimeoutError("pooler Supabase")

    monkeypatch.setattr(wa_discover_worker, "esegui_discover_run", finto_motore)
    monkeypatch.setattr(wa_discover_worker, "AsyncSessionLocal",
                        _sessione_finta(db_session))
    monkeypatch.setattr(wa_discover_runs, "chiudi_run", chiudi_run_che_esplode)

    # Non deve sollevare: se lo fa, questo test fallisce con l'eccezione
    # stessa (nessun assert extra necessario per dimostrare la propagazione).
    await wa_discover_worker.wa_discover_task({}, number.id, run.id)


@pytest.mark.asyncio
async def test_se_la_chiusura_fallisce_il_secondo_tentativo_marca_failed(
        db_session, monkeypatch):
    # Il retry "a mani nude" non e' solo per non sollevare: deve provare
    # DAVVERO a chiudere la run. Qui il primo chiudi_run fallisce (blip
    # transitorio), il secondo (chiudi_run vero) riesce.
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    run = await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id,
                                          number_id=number.id)
    await db_session.commit()

    async def finto_motore(number_id, **kw):
        return {"salvate": 1, "motivo": "completato"}

    chiudi_run_vero = wa_discover_runs.chiudi_run
    chiamate = {"n": 0}

    async def chiudi_run_fallisce_la_prima_volta(*a, **kw):
        chiamate["n"] += 1
        if chiamate["n"] == 1:
            raise TimeoutError("pooler Supabase")
        return await chiudi_run_vero(*a, **kw)

    monkeypatch.setattr(wa_discover_worker, "esegui_discover_run", finto_motore)
    monkeypatch.setattr(wa_discover_worker, "AsyncSessionLocal",
                        _sessione_finta(db_session))
    monkeypatch.setattr(wa_discover_runs, "chiudi_run",
                        chiudi_run_fallisce_la_prima_volta)

    await wa_discover_worker.wa_discover_task({}, number.id, run.id)

    assert chiamate["n"] == 2
    chiusa = await wa_discover_runs.ultima_run(db_session, number.id)
    assert chiusa.stato == "failed"


@pytest.mark.asyncio
async def test_il_task_committa_la_chiusura_della_run(db_session, monkeypatch):
    # _sessione_finta condivide la stessa sessione/transazione del test: le
    # scritture Core di chiudi_run sono gia' visibili qui anche SENZA
    # commit (nessun rollback: il fake __aexit__ non lo fa). In produzione
    # AsyncSessionLocal() e' una sessione VERA e un `async with` senza
    # commit fa rollback -- questo test verifica che il commit sia
    # DAVVERO chiamato, non solo che la lettura successiva torni giusta
    # (quella da sola non si accorgerebbe di un commit mancante).
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    run = await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id,
                                          number_id=number.id)
    await db_session.commit()

    async def finto_motore(number_id, **kw):
        return {"salvate": 1, "motivo": "completato"}

    tracciato = {"chiamato": False}
    monkeypatch.setattr(wa_discover_worker, "esegui_discover_run", finto_motore)
    monkeypatch.setattr(wa_discover_worker, "AsyncSessionLocal",
                        _sessione_finta(db_session, tracciato))

    await wa_discover_worker.wa_discover_task({}, number.id, run.id)

    assert tracciato["chiamato"] is True


def _sessione_finta(db_session, tracciato: dict | None = None):
    """Stessa sessione/transazione del test intero (non un engine separato):
    un `async with` che non fa ne' commit ne' rollback all'uscita, per
    poter leggere subito dopo con lo stesso db_session del test.

    Se `tracciato` e' passato, registra se `.commit()` e' stato chiamato sul
    db passato al worker: serve al test che dimostra che quel commit e'
    davvero necessario (senza questo tracciamento, un test che togliesse
    `await db.commit()` da wa_discover_task resterebbe verde lo stesso,
    perche' qui non c'e' nessun rollback a nascondere l'omissione).
    """
    commit_originale = db_session.commit

    async def _commit_tracciato():
        if tracciato is not None:
            tracciato["chiamato"] = True
        await commit_originale()

    class _Ctx:
        async def __aenter__(self):
            db_session.commit = _commit_tracciato
            return db_session

        async def __aexit__(self, *a):
            return False

    return lambda: _Ctx()
