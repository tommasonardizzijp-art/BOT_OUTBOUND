"""Non-regressione: il canale IG e' in produzione. Si scrive PRIMA di
toccare task_queue.py (Task 12, brief Step 1)."""
import uuid

from app.utils.tempo import adesso_utc

import pytest

from tests.factories_wa import (make_campaign, make_campaign_contact,
                                make_contact, make_number, make_tenant)


def _nome_funzione_registrata(f) -> str:
    """Alcune funzioni sono registrate nude, altre avvolte in `arq.worker.func(...)`
    (es. browser_bio_account_task per il max_tries alto): l'oggetto risultante e'
    un `arq.worker.Function`, che espone `.name` e NON `__name__`. Senza questa
    distinzione il confronto sui nomi fallisce sempre per le funzioni avvolte,
    a prescindere da cosa sia davvero registrato -- falso negativo perenne."""
    return getattr(f, "name", None) or getattr(f, "__name__", None) or str(f)


def test_le_funzioni_instagram_restano_registrate():
    """Non-regressione: il canale IG e' in produzione. Si scrive PRIMA di
    toccare task_queue.py."""
    from app.workers.task_queue import WorkerSettings
    nomi = {_nome_funzione_registrata(f) for f in WorkerSettings.functions}
    for atteso in ("pre_generate_messages_task", "full_batch_generate_task",
                   "browser_bio_account_task", "browser_import_account_task"):
        assert atteso in nomi, f"{atteso} sparita dalla registrazione ARQ"


def test_wa_send_task_e_registrata():
    from app.workers.task_queue import WorkerSettings
    nomi = {_nome_funzione_registrata(f) for f in WorkerSettings.functions}
    assert "wa_send_task" in nomi


def test_job_id_e_per_numero_non_per_campagna():
    from app.workers.wa_worker import wa_send_job_id
    assert wa_send_job_id("num-1") == "wa:send:num-1"
    assert wa_send_job_id("num-1") == wa_send_job_id("num-1")   # deterministico


async def _scenario_messaggio_sending(db_session):
    """Tenant + numero + contatto + campagna + riga wa_campaign_contacts
    LOCKATA (worker "vivo" al momento del crash) + un wa_messages 'sending':
    esattamente lo stato che il processo lascia a meta' di un invio (FM14)."""
    from app.models.wa import WaMessage, WaMessageStatus

    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    contact = await make_contact(db_session, tenant)
    campaign, step = await make_campaign(db_session, tenant, number)
    cc = await make_campaign_contact(db_session, campaign, contact)
    cc.locked_by = f"wa-{uuid.uuid4().hex[:6]}"
    # locked_at e' timestamptz (models/wa.py): un naive scritto da un processo
    # su Europe/Rome finisce a DB due ore indietro. Qui non si vede -- la suite
    # gira su SQLite, che restituisce naive qualunque cosa gli si dia -- ed e'
    # proprio per questo che va scritto giusto: e' una riga da cui si copia.
    cc.locked_at = adesso_utc()
    await db_session.commit()

    msg = WaMessage(id=str(uuid.uuid4()), campaign_id=campaign.id,
                    contact_id=contact.id, wa_number_id=number.id,
                    step_index=step.step_index, template_variant="a",
                    rendered_text="Ciao Marco, promo attiva.",
                    status=WaMessageStatus.sending)
    db_session.add(msg)
    await db_session.commit()

    return {"tenant": tenant, "number": number, "contact": contact,
            "campaign": campaign, "step": step, "cc": cc, "msg": msg}


@pytest.mark.asyncio
async def test_recover_wa_sending_riporta_a_queued_i_messaggi_appesi(db_session):
    """FM14: il PC si riavvia a meta' invio. Un wa_messages 'sending' senza
    processo vivo e' lavoro appeso: il messaggio va failed e il contatto
    corrispondente si ferma (skipped), mai riprovato per questo tentativo
    ambiguo (decisione Tommaso round1)."""
    from sqlalchemy import select
    from app.models.wa import WaContactStatus, WaMessage, WaMessageStatus
    from app.workers.wa_worker import recover_wa_sending_on_startup

    ctx = await _scenario_messaggio_sending(db_session)
    n = await recover_wa_sending_on_startup()
    assert n == 1
    msg = await db_session.scalar(select(WaMessage).where(WaMessage.id == ctx["msg"].id))
    await db_session.refresh(msg)
    assert msg.status == WaMessageStatus.failed
    assert "recovery" in (msg.error or "")
    await db_session.refresh(ctx["cc"])
    assert ctx["cc"].locked_by is None
    assert ctx["cc"].status == WaContactStatus.skipped
    await db_session.refresh(ctx["contact"])
    assert ctx["contact"].do_not_contact is False


def test_on_startup_registrato_chiama_la_recovery_wa(monkeypatch):
    """La recovery FM14 esisteva ed era testata da Task 3, ma nessuno la
    chiamava in produzione (I3, whole-branch review). Decisione Tommaso
    round1: deve girare ad OGNI avvio del worker ARQ, in aggiunta -- non al
    posto -- della guardia IG gia' esistente."""
    import asyncio
    from app.workers import task_queue

    chiamato = {"wa": False, "ig": False}

    async def _fake_wa():
        chiamato["wa"] = True
        return 0
    monkeypatch.setattr(task_queue, "recover_wa_sending_on_startup", _fake_wa)

    async def _fake_ig():
        chiamato["ig"] = True
        return {"campaigns_paused": 0, "locks_released": 0, "leases_released": 0}
    monkeypatch.setattr("app.services.work_enqueue.pause_active_work_on_startup", _fake_ig)

    asyncio.run(task_queue.on_startup({}))

    assert chiamato["wa"] is True
    assert chiamato["ig"] is True


@pytest.mark.asyncio
async def test_wa_send_task_rischedula_dopo_il_break_su_motivo_non_terminale(monkeypatch):
    """cap_esaurito / fuori_finestra / completata -> si riprende dopo il
    break: non e' un motivo che chiude la sessione (contrario ai motivi
    testati sotto), quindi wa_send_task deve sollevare Retry(defer=...) col
    defer del break (FM11 -- ARQ rischedula lo STESSO job dopo che questa
    invocazione e' uscita).

    NON piu' un enqueue_job manuale con lo stesso _job_id (Fix A, review
    finale round 2): chiamato da dentro il job ancora in esecuzione,
    tornerebbe None in silenzio -- la chiave arq:job:{job_id} scritta
    all'enqueue originale resta viva fino a finish_job, che gira DOPO il
    return di questa coroutine, quindi ARQ vede il job "gia' in coda" e
    scarta il duplicato. Retry evita il problema: non chiama enqueue_job."""
    from arq.worker import Retry
    from app.workers import wa_worker

    async def _fake_mini_sessione(number_id):
        return {"inviati": 3, "falliti": 0, "saltati": 0, "motivo": "cap_esaurito"}
    monkeypatch.setattr(wa_worker, "esegui_mini_sessione", _fake_mini_sessione)
    monkeypatch.setattr(wa_worker.wa_timing, "wa_session_break_seconds", lambda campaign: 42.0)

    with pytest.raises(Retry) as exc_info:
        await wa_worker.wa_send_task({}, "num-1")
    assert exc_info.value.defer_score == 42000   # ms


@pytest.mark.asyncio
async def test_wa_send_task_non_rischedula_su_motivo_terminale(monkeypatch):
    """send_disabled / wa_halted / numero_non_attivo / guasti_consecutivi /
    niente_da_fare -> la sessione si chiude e basta: nessun Retry, altrimenti
    si ririproverebbe un kill-switch attivo o un numero fermato per guasto,
    ignorando il motivo per cui si e' fermato."""
    from app.workers import wa_worker

    async def _fake_mini_sessione(number_id):
        return {"inviati": 0, "falliti": 0, "saltati": 0, "motivo": "wa_halted"}
    monkeypatch.setattr(wa_worker, "esegui_mini_sessione", _fake_mini_sessione)

    chiamato = {"si": False}
    async def _fake_pool(*a, **kw):
        chiamato["si"] = True
        raise AssertionError("non deve aprire un pool Redis su motivo terminale")
    monkeypatch.setattr(wa_worker.arq, "create_pool", _fake_pool)

    await wa_worker.wa_send_task({}, "num-1")   # non deve sollevare Retry
    assert chiamato["si"] is False
