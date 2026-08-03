import uuid
from datetime import datetime, timedelta

import pytest

from app.models.wa import WaNumberStatus
from tests.factories_wa import make_number, make_tenant


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
async def test_healthcheck_salta_numero_con_profilo_occupato(db_session, monkeypatch):
    from app.workers import cron_worker
    from app.services import wa_profile_lock

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant, status=WaNumberStatus.active)
    await db_session.commit()

    class _CtxOccupato:
        def __call__(self, number_id, ttl_min=None):
            self._number_id = number_id
            return self

        async def __aenter__(self):
            raise wa_profile_lock.WaProfileBusy(self._number_id)

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(cron_worker.wa_profile_lock, "held", _CtxOccupato())

    async def _fake_check(number_id):
        raise AssertionError("check_session non deve essere chiamato se il profilo e' occupato")
    monkeypatch.setattr(cron_worker, "check_session", _fake_check)

    esito = await cron_worker.wa_session_healthcheck({})
    # >= 1 e non == 1: il DB di test e' condiviso fra i test del file (niente
    # rollback perche' _scenario_claim/make_number committano davvero), quindi
    # i numeri creati dai test precedenti sono ancora attivi e vengono anche
    # loro saltati (il lock e' occupato per QUALSIASI number_id qui).
    assert esito["saltati_invio_attivo"] >= 1
    assert esito["controllati"] == 0


@pytest.mark.asyncio
async def test_healthcheck_controlla_numero_con_profilo_libero(db_session, monkeypatch):
    from app.workers import cron_worker

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant, status=WaNumberStatus.active)
    await db_session.commit()

    chiamati = []

    async def _fake_check(number_id):
        chiamati.append(number_id)
        return WaNumberStatus.active
    monkeypatch.setattr(cron_worker, "check_session", _fake_check)

    esito = await cron_worker.wa_session_healthcheck({})
    # numero.id nei chiamati (non esito["controllati"] == 1): il DB di test e'
    # condiviso fra i test del file, quindi altri numeri gia' committati da
    # test precedenti vengono controllati anche loro.
    assert numero.id in chiamati
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
