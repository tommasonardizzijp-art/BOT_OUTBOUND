import uuid
from datetime import datetime, timedelta

import pytest


async def _scenario_claim(db_session, e164: str = "+393331112223", contatti: int = 1):
    """Tenant + numero + contatto + campagna running + step 0, tutto a DB.
    Copiato da test_wa_worker.py (stessa scelta li': copiato invece di
    importato, cosi' questo file resta leggibile da solo)."""
    from app.models.tenant import Tenant
    from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                               WaCampaignType, WaContact, WaContactStatus, WaNumber,
                               WaNumberStatus, WaSendCondition, WaSequenceStep)
    from app.utils.crypto import encrypt
    from app.utils.phone_pseudonym import hmac_phone

    tenant = Tenant(id=str(uuid.uuid4()), name="T", status="active")
    db_session.add(tenant)
    await db_session.flush()
    number = WaNumber(id=str(uuid.uuid4()), tenant_id=tenant.id, label="n",
                      phone_hmac=f"n-{uuid.uuid4()}", encrypted_phone=encrypt("+390000000000"),
                      status=WaNumberStatus.active)
    db_session.add(number)
    await db_session.flush()
    campaign = WaCampaign(id=str(uuid.uuid4()), tenant_id=tenant.id,
                          wa_number_id=number.id, name="c",
                          campaign_type=WaCampaignType.marketing,
                          status=WaCampaignStatus.running, optout_enabled=True,
                          optout_cta="Scrivi STOP per non ricevere piu' messaggi.")
    db_session.add(campaign)
    await db_session.flush()
    step = WaSequenceStep(id=str(uuid.uuid4()), campaign_id=campaign.id, step_index=0,
                          template_a="Ciao {nome}, promo attiva.",
                          send_condition=WaSendCondition.always, wait_days=0)
    db_session.add(step)
    await db_session.flush()

    contacts, ccs = [], []
    for i in range(contatti):
        e = e164 if i == 0 else f"{e164}{i}"
        c = WaContact(id=str(uuid.uuid4()), tenant_id=tenant.id,
                     phone_hmac=hmac_phone(e), encrypted_phone=encrypt(e),
                     display_name="Marco")
        db_session.add(c)
        await db_session.flush()
        cc = WaCampaignContact(id=str(uuid.uuid4()), campaign_id=campaign.id,
                               contact_id=c.id, status=WaContactStatus.queued,
                               current_step=-1,
                               next_action_at=datetime.utcnow() - timedelta(minutes=1))
        db_session.add(cc)
        contacts.append(c)
        ccs.append(cc)
    await db_session.commit()
    return {"tenant": tenant, "number": number, "contact": contacts[0],
            "campaign": campaign, "step": step, "cc": ccs[0],
            "contacts": contacts, "ccs": ccs}


@pytest.mark.asyncio
async def test_healthcheck_mette_in_pausa_le_campagne_del_numero_caduto(db_session, monkeypatch):
    from app.models.wa import WaCampaignStatus, WaNumberStatus
    from app.workers import cron_worker

    ctx = await _scenario_claim(db_session)

    async def _fake_check(number_id):
        return WaNumberStatus.qr_required
    monkeypatch.setattr(cron_worker, "check_session", _fake_check)

    await cron_worker.wa_session_healthcheck({})
    await db_session.refresh(ctx["campaign"])
    assert ctx["campaign"].status == WaCampaignStatus.paused


@pytest.mark.asyncio
async def test_healthcheck_non_tocca_le_campagne_se_la_sessione_e_viva(db_session, monkeypatch):
    from app.models.wa import WaCampaignStatus, WaNumberStatus
    from app.workers import cron_worker

    ctx = await _scenario_claim(db_session)

    async def _fake_check(number_id):
        return WaNumberStatus.active
    monkeypatch.setattr(cron_worker, "check_session", _fake_check)

    await cron_worker.wa_session_healthcheck({})
    await db_session.refresh(ctx["campaign"])
    assert ctx["campaign"].status == WaCampaignStatus.running


@pytest.mark.asyncio
async def test_healthcheck_rilascia_i_lock_stale(db_session, monkeypatch):
    from datetime import datetime, timedelta
    from app.models.wa import WaNumberStatus
    from app.workers import cron_worker

    ctx = await _scenario_claim(db_session)
    ctx["cc"].locked_by = "worker-morto"
    ctx["cc"].locked_at = datetime.utcnow() - timedelta(minutes=45)
    await db_session.commit()

    async def _fake_check(number_id):
        return WaNumberStatus.active
    monkeypatch.setattr(cron_worker, "check_session", _fake_check)

    await cron_worker.wa_session_healthcheck({})
    await db_session.refresh(ctx["cc"])
    assert ctx["cc"].locked_by is None


@pytest.mark.asyncio
async def test_19_healthcheck_avvisa_telegram_su_sessione_caduta(db_session, monkeypatch):
    """QA item 19: l'alert Telegram e' verificabile mockando il notifier,
    non serve un bot reale."""
    from app.models.wa import WaNumberStatus
    from app.services import notifier
    from app.workers import cron_worker

    ctx = await _scenario_claim(db_session)

    async def _fake_check(number_id):
        return WaNumberStatus.qr_required
    monkeypatch.setattr(cron_worker, "check_session", _fake_check)

    chiamate = []

    async def _fake_telegram(msg, level="info"):
        chiamate.append((msg, level))
    monkeypatch.setattr(notifier, "send_telegram", _fake_telegram)

    esito = await cron_worker.wa_session_healthcheck({})
    assert esito["caduti"] >= 1
    assert len(chiamate) >= 1
    assert chiamate[0][1] == "error"
    assert "WhatsApp" in chiamate[0][0]


@pytest.mark.asyncio
async def test_healthcheck_salta_numero_con_wa_send_task_attivo(db_session, monkeypatch):
    """Fix 1 (review finale, Critical): cron health-check e worker di invio
    sono due processi ARQ separati che possono aprire lo STESSO profilo
    Chromium in parallelo (_open_wa_browser cancella i lock OS del profilo
    come parte del suo cleanup, quindi il secondo avvio SUCCEDE invece di
    fallire -- corruzione profilo, sessione WA persa). Se wa_send_task e'
    queued/in_progress per un numero, l'health-check lo salta invece di
    chiamare check_session su quello stesso numero."""
    from app.models.wa import WaNumberStatus
    from app.workers import cron_worker

    ctx = await _scenario_claim(db_session)

    chiamati = []

    async def _fake_check(number_id):
        chiamati.append(number_id)
        return WaNumberStatus.active
    monkeypatch.setattr(cron_worker, "check_session", _fake_check)

    async def _fake_job_attivo(redis, number_id):
        return number_id == ctx["number"].id
    monkeypatch.setattr(cron_worker, "_wa_send_job_is_active", _fake_job_attivo)

    class _FakeRedis:
        async def aclose(self):
            pass

    async def _fake_pool(*a, **kw):
        return _FakeRedis()
    monkeypatch.setattr(cron_worker.arq, "create_pool", _fake_pool)

    esito = await cron_worker.wa_session_healthcheck({})
    assert ctx["number"].id not in chiamati
    assert esito["saltati_invio_attivo"] >= 1


@pytest.mark.asyncio
async def test_healthcheck_controlla_numero_senza_job_attivo(db_session, monkeypatch):
    """Contro-prova: un numero SENZA wa_send_task attivo viene controllato
    normalmente -- il fix non deve fermare tutto il health-check."""
    from app.models.wa import WaNumberStatus
    from app.workers import cron_worker

    ctx = await _scenario_claim(db_session)

    chiamati = []

    async def _fake_check(number_id):
        chiamati.append(number_id)
        return WaNumberStatus.active
    monkeypatch.setattr(cron_worker, "check_session", _fake_check)

    async def _fake_job_attivo(redis, number_id):
        return False
    monkeypatch.setattr(cron_worker, "_wa_send_job_is_active", _fake_job_attivo)

    class _FakeRedis:
        async def aclose(self):
            pass

    async def _fake_pool(*a, **kw):
        return _FakeRedis()
    monkeypatch.setattr(cron_worker.arq, "create_pool", _fake_pool)

    esito = await cron_worker.wa_session_healthcheck({})
    assert ctx["number"].id in chiamati
    assert esito["saltati_invio_attivo"] == 0


def test_i_cron_instagram_restano_registrati_e_healthcheck_wa_e_aggiunto():
    """Non-regressione: cron_worker.py e' condiviso con Instagram in
    produzione. Ogni entry di cron_jobs e' un arq.cron.CronJob: il nome
    reale sta in .coroutine.__name__ (nessuna wrapping alternativa in
    questo file, verificato — a differenza di task_queue.py::WorkerSettings
    .functions, che mischia funzioni nude e arq.worker.func(...))."""
    from app.workers import cron_worker

    nomi = {job.coroutine.__name__ for job in cron_worker.CronWorkerSettings.cron_jobs}
    for atteso in ("daily_reset", "release_stale_locks", "check_replies",
                   "recover_sending", "telegram_commands"):
        assert atteso in nomi, f"{atteso} sparita dai cron IG"
    assert "wa_session_healthcheck" in nomi
