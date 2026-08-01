"""Non-regressione: il canale IG e' in produzione. Si scrive PRIMA di
toccare task_queue.py (Task 12, brief Step 1)."""
import uuid

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
    from datetime import datetime
    cc.locked_at = datetime.utcnow()
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
    processo vivo e' lavoro appeso: si riapre, non si perde."""
    from sqlalchemy import select
    from app.models.wa import WaMessage, WaMessageStatus
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


class _FakeRedisEnqueue:
    """Stesso fake minimale di test_wa_number_manager.test_apply_and_release_wa_cooldown:
    registra la enqueue_job invece di aprire un pool Redis vero."""
    def __init__(self, calls):
        self._calls = calls

    async def enqueue_job(self, task, *args, **kwargs):
        self._calls["task"] = task
        self._calls["args"] = args
        self._calls["kwargs"] = kwargs
        return object()   # job non-None: enqueue_wa_workers lo conta come riuscito

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_wa_send_task_rischedula_dopo_il_break_su_motivo_non_terminale(monkeypatch):
    """cap_esaurito / fuori_finestra / completata -> si riprende dopo il
    break: non e' un motivo che chiude la sessione (contrario ai motivi
    testati sotto), quindi wa_send_task deve rimettersi in coda da solo
    sullo STESSO job_id (FM11) col defer del break."""
    from app.workers import wa_worker

    async def _fake_mini_sessione(number_id):
        return {"inviati": 3, "falliti": 0, "saltati": 0, "motivo": "cap_esaurito"}
    monkeypatch.setattr(wa_worker, "esegui_mini_sessione", _fake_mini_sessione)
    monkeypatch.setattr(wa_worker.wa_timing, "wa_session_break_seconds", lambda campaign: 42.0)

    calls = {}
    async def _fake_pool(*a, **kw):
        return _FakeRedisEnqueue(calls)
    monkeypatch.setattr(wa_worker.arq, "create_pool", _fake_pool)

    await wa_worker.wa_send_task({}, "num-1")

    assert calls["task"] == "wa_send_task"
    assert calls["args"] == ("num-1",)
    assert calls["kwargs"]["_job_id"] == wa_worker.wa_send_job_id("num-1")
    assert calls["kwargs"]["_defer_by"] == 42


@pytest.mark.asyncio
async def test_wa_send_task_non_rischedula_su_motivo_terminale(monkeypatch):
    """send_disabled / wa_halted / numero_non_attivo / guasti_consecutivi /
    niente_da_fare -> la sessione si chiude e basta: rimettersi in coda
    subito significherebbe ririprovare un kill-switch attivo o un numero
    fermato per guasto, ignorando il motivo per cui si e' fermato."""
    from app.workers import wa_worker

    async def _fake_mini_sessione(number_id):
        return {"inviati": 0, "falliti": 0, "saltati": 0, "motivo": "wa_halted"}
    monkeypatch.setattr(wa_worker, "esegui_mini_sessione", _fake_mini_sessione)

    chiamato = {"si": False}
    async def _fake_pool(*a, **kw):
        chiamato["si"] = True
        raise AssertionError("non deve aprire un pool Redis su motivo terminale")
    monkeypatch.setattr(wa_worker.arq, "create_pool", _fake_pool)

    await wa_worker.wa_send_task({}, "num-1")
    assert chiamato["si"] is False
