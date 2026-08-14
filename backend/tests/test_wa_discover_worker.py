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


def test_il_task_e_registrato_nel_worker():
    # Un job non registrato viene accodato e non parte mai: la run resta
    # 'running' e il numero non e' piu' scansionabile.
    from app.workers.task_queue import WorkerSettings

    nomi = {getattr(f, "__name__", getattr(f, "coroutine", None) and f.coroutine.__name__)
            for f in WorkerSettings.functions}
    assert "wa_discover_task" in nomi


def _sessione_finta(db_session):
    class _Ctx:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *a):
            return False

    return lambda: _Ctx()
