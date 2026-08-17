"""Factory dei test del canale WA, condivise fra M2 e M3 (contratto §5.1).
Modulo normale, NON un conftest: cosi' nessuno dei due cantieri ha motivo
di toccare backend/tests/conftest.py, che dopo PR-0 e' congelato."""
import uuid
from datetime import datetime

from app.utils.tempo import adesso_utc

from app.models.tenant import Tenant
from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                           WaCampaignType, WaContact, WaContactStatus,
                           WaDiscoveredChat, WaDiscoverRun, WaNumber,
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
        started_at=adesso_utc() if status == WaCampaignStatus.running else None,
    )
    db.add(camp)
    await db.flush()
    step = WaSequenceStep(id=str(uuid.uuid4()), campaign_id=camp.id, step_index=0,
                          template_a=template, send_condition=WaSendCondition.always,
                          wait_days=0)
    db.add(step)
    await db.flush()
    return camp, step


async def make_discovered_chat(db, tenant, number, *,
                               chat_title: str | None = "Chat Test",
                               display_name: str | None = "Chat Test",
                               e164: str | None = "auto",
                               tipo_chat: str = "individuale",
                               status: str = "nuovo",
                               numero_leggibile: bool | None = None) -> WaDiscoveredChat:
    """Una riga di staging (Fase A auto-discover), patrimonio condiviso a
    partire dalla Fase B (wa_promote) -- aggiunta qui, non nel file di test,
    perche' Task 4 (API GET/POST discovered-chats) e i test adversarial del
    modulo la riuseranno di sicuro, stesso ragionamento che ha messo
    make_contact/make_campaign qui invece che nei singoli file di test.

    `e164="auto"` (sentinella, non None) genera un numero random univoco per
    lo stesso motivo di `make_number`/`make_contact`: un default fisso
    farebbe collidere test diversi sulla UNIQUE(number_id, phone_hmac).
    Passare `e164=None` esplicito per una riga SENZA numero (gruppo, o
    pannello info non apribile) -- a differenza di WaContact qui e' un caso
    legittimo, le due colonne sono NULLABLE.
    """
    if e164 == "auto":
        e164 = f"+3935{uuid.uuid4().int % 10**8:08d}"
    encrypted_phone = encrypt(e164) if e164 else None
    phone_hmac = hmac_phone(e164) if e164 else None
    if numero_leggibile is None:
        numero_leggibile = phone_hmac is not None
    riga = WaDiscoveredChat(
        id=str(uuid.uuid4()), tenant_id=tenant.id, number_id=number.id,
        chat_title=chat_title, display_name=display_name,
        encrypted_phone=encrypted_phone, phone_hmac=phone_hmac,
        numero_leggibile=numero_leggibile, tipo_chat=tipo_chat, status=status,
    )
    db.add(riga)
    await db.flush()
    return riga


async def make_discover_run(db, tenant, number, *, stato: str = "running",
                            avviato_da: str = "manuale", salvate: int = 0,
                            aggiornate: int = 0, saltate_gia_note: int = 0,
                            non_verificate: int = 0, dichiarato: int | None = None,
                            copertura: int | None = None, motivo: str = "in_corso",
                            sync_stato: str = "ignota") -> WaDiscoverRun:
    run = WaDiscoverRun(
        id=str(uuid.uuid4()), tenant_id=tenant.id, number_id=number.id,
        stato=stato, avviato_da=avviato_da, salvate=salvate, aggiornate=aggiornate,
        saltate_gia_note=saltate_gia_note, non_verificate=non_verificate,
        dichiarato=dichiarato, copertura=copertura, motivo=motivo,
        sync_stato=sync_stato,
    )
    db.add(run)
    await db.flush()
    return run


async def make_campaign_contact(db, campaign, contact, *,
                                status=WaContactStatus.queued,
                                current_step: int = -1) -> WaCampaignContact:
    """Rispetta il contratto di consegna §7.1: next_action_at NON e' mai
    NULL su una riga non terminale, e i campi di lock restano vuoti (I1)."""
    cc = WaCampaignContact(id=str(uuid.uuid4()), campaign_id=campaign.id,
                           contact_id=contact.id, status=status, current_step=current_step,
                           next_action_at=adesso_utc(), failure_count=0)
    db.add(cc)
    await db.flush()
    return cc
