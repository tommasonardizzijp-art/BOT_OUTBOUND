"""Factory dei test del canale WA, condivise fra M2 e M3 (contratto §5.1).
Modulo normale, NON un conftest: cosi' nessuno dei due cantieri ha motivo
di toccare backend/tests/conftest.py, che dopo PR-0 e' congelato."""
import uuid
from datetime import datetime

from app.models.tenant import Tenant
from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                           WaCampaignType, WaContact, WaContactStatus, WaNumber,
                           WaNumberStatus, WaSendCondition, WaSequenceStep)
from app.utils.crypto import encrypt
from app.utils.phone_pseudonym import hmac_phone


async def make_tenant(db, name: str = "Tenant Test") -> Tenant:
    t = Tenant(id=str(uuid.uuid4()), name=name, status="active")
    db.add(t)
    await db.flush()
    return t


async def make_number(db, tenant, *, label="Numero Test",
                      e164: str | None = None, status=WaNumberStatus.active) -> WaNumber:
    # phone_hmac e' UNIQUE GLOBALE: un numero fisso qui farebbe collidere
    # test diversi con un errore che sembra una regressione.
    e164 = e164 or f"+3933{uuid.uuid4().int % 10**8:08d}"
    n = WaNumber(id=str(uuid.uuid4()), tenant_id=tenant.id, label=label,
                 phone_hmac=hmac_phone(e164), encrypted_phone=encrypt(e164),
                 status=status, daily_cap=20, warmup_day=1)
    db.add(n)
    await db.flush()
    return n


async def make_contact(db, tenant, *, e164: str | None = None,
                       display_name: str | None = "Marco",
                       attributes: dict | None = None) -> WaContact:
    e164 = e164 or f"+3934{uuid.uuid4().int % 10**8:08d}"
    c = WaContact(id=str(uuid.uuid4()), tenant_id=tenant.id,
                  phone_hmac=hmac_phone(e164), encrypted_phone=encrypt(e164),
                  display_name=display_name, attributes=attributes)
    db.add(c)
    await db.flush()
    return c


async def make_campaign(db, tenant, number, *, name="Campagna Test",
                        tipo=WaCampaignType.marketing,
                        status=WaCampaignStatus.draft,
                        template="Ciao {nome}, promo attiva.") -> tuple:
    camp = WaCampaign(
        id=str(uuid.uuid4()), tenant_id=tenant.id, wa_number_id=number.id, name=name,
        campaign_type=tipo, status=status,
        optout_enabled=(tipo == WaCampaignType.marketing),
        optout_cta=("Scrivi STOP per non ricevere piu' messaggi."
                    if tipo == WaCampaignType.marketing else None),
        started_at=datetime.utcnow() if status == WaCampaignStatus.running else None,
    )
    db.add(camp)
    await db.flush()
    step = WaSequenceStep(id=str(uuid.uuid4()), campaign_id=camp.id, step_index=0,
                          template_a=template, send_condition=WaSendCondition.always,
                          wait_days=0)
    db.add(step)
    await db.flush()
    return camp, step


async def make_campaign_contact(db, campaign, contact, *,
                                status=WaContactStatus.queued) -> WaCampaignContact:
    """Rispetta il contratto di consegna §7.1: next_action_at NON e' mai
    NULL su una riga non terminale, e i campi di lock restano vuoti (I1)."""
    cc = WaCampaignContact(id=str(uuid.uuid4()), campaign_id=campaign.id,
                           contact_id=contact.id, status=status, current_step=-1,
                           next_action_at=datetime.utcnow(), failure_count=0)
    db.add(cc)
    await db.flush()
    return cc
