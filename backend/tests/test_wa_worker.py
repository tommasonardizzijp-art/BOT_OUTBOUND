import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

from app.workers import wa_worker


async def _scenario_claim(db_session, e164: str = "+393331112223"):
    """Tenant + numero + contatto + campagna running + step 0, tutto a DB.
    Identico a _scenario_invio del Task 8 (test_wa_sender.py) piu'
    next_action_at nel passato sulla riga wa_campaign_contacts: copiato
    apposta invece di importato, cosi' questo file resta leggibile da solo."""
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
    contact = WaContact(id=str(uuid.uuid4()), tenant_id=tenant.id,
                        phone_hmac=hmac_phone(e164), encrypted_phone=encrypt(e164),
                        display_name="Marco")
    db_session.add_all([number, contact])
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
    cc = WaCampaignContact(id=str(uuid.uuid4()), campaign_id=campaign.id,
                           contact_id=contact.id, status=WaContactStatus.queued,
                           current_step=-1,
                           next_action_at=datetime.utcnow() - timedelta(minutes=1))
    db_session.add_all([step, cc])
    await db_session.commit()
    return {"tenant": tenant, "number": number, "contact": contact,
            "campaign": campaign, "step": step, "cc": cc}


@pytest.mark.asyncio
async def test_claim_prende_la_riga_pronta(db_session):
    ctx = await _scenario_claim(db_session)
    preso = await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1")
    assert preso is not None
    cc, contact, campaign, step = preso
    assert cc.id == ctx["cc"].id
    assert cc.locked_by == "w1"
    assert step.step_index == 0


@pytest.mark.asyncio
async def test_claim_ignora_next_action_at_null(db_session):
    """Invariante I3 del contratto: una riga senza appuntamento non e' una
    riga da inviare subito, e' una riga rotta. Fail-closed."""
    ctx = await _scenario_claim(db_session)
    ctx["cc"].next_action_at = None
    await db_session.commit()
    assert await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1") is None


@pytest.mark.asyncio
async def test_claim_ignora_contatto_optato_fuori(db_session):
    """M2 filtra all'ingest, ma fra ingest e invio passano settimane
    (invariante I4): si ricontrolla live."""
    ctx = await _scenario_claim(db_session)
    ctx["contact"].opted_out = True
    await db_session.commit()
    assert await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1") is None


@pytest.mark.asyncio
async def test_claim_ignora_campagna_non_running_e_numero_non_active(db_session):
    from app.models.wa import WaCampaignStatus, WaNumberStatus
    ctx = await _scenario_claim(db_session)
    ctx["campaign"].status = WaCampaignStatus.paused
    await db_session.commit()
    assert await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1") is None

    ctx["campaign"].status = WaCampaignStatus.running
    ctx["number"].status = WaNumberStatus.qr_required
    await db_session.commit()
    assert await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1") is None


@pytest.mark.asyncio
async def test_claim_rispetta_lock_fresco_e_recupera_lock_stale(db_session):
    ctx = await _scenario_claim(db_session)
    ctx["cc"].locked_by = "altro-worker"
    ctx["cc"].locked_at = datetime.utcnow()
    await db_session.commit()
    assert await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1") is None

    # lock vecchio di 21 minuti: la sessione che lo teneva e' morta
    ctx["cc"].locked_at = datetime.utcnow() - timedelta(minutes=21)
    await db_session.commit()
    preso = await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1")
    assert preso is not None and preso[0].locked_by == "w1"


@pytest.mark.asyncio
async def test_due_worker_concorrenti_non_prendono_la_stessa_riga(db_session):
    """Concorrenza VERA (gather), non due chiamate in fila: e' l'unico modo
    in cui il bug si manifesta."""
    from app.database import AsyncSessionLocal
    ctx = await _scenario_claim(db_session)

    async def _claim(worker_id):
        async with AsyncSessionLocal() as db:
            return await wa_worker.claim_next_wa_contact(
                db, number_id=ctx["number"].id, worker_id=worker_id)

    a, b = await asyncio.gather(_claim("w1"), _claim("w2"))
    assert (a is None) != (b is None), "esattamente uno dei due deve vincere"


@pytest.mark.asyncio
async def test_claim_ignora_contatto_oltre_soglia_fallimenti(db_session, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "wa_max_failures_per_contact", 3)
    ctx = await _scenario_claim(db_session)
    ctx["cc"].failure_count = 3
    await db_session.commit()
    assert await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1") is None
