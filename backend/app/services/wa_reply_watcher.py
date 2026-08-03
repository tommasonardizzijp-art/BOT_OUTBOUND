"""Reply-watcher del canale WhatsApp (SDD §7.3): legge SOLO la lista chat
(sidebar), mai apre una conversazione -- aprirla marcherebbe "letto" e
brucerebbe le notifiche del cliente sul telefono (vincolo di coesistenza,
SDD §9). Matching contatto, dedup eventi, dispatch opt-out/replied.
"""
from datetime import datetime

from sqlalchemy import func, select

from app.browser.whatsapp_page import ChatRow
from app.config import settings
from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                           WaContact, WaContactStatus, WaInboundEvent,
                           WaMatchedBy, WaNumber, WaNumberStatus)
from app.services import wa_optout
from app.utils import events
from app.utils.phone_pseudonym import PhoneNormalizationError, hmac_phone, normalize_e164


async def match_contact(db, tenant_id: str, row: ChatRow) -> tuple[WaContact | None, WaMatchedBy]:
    """Tre livelli, in ordine, mai indovinare (SDD §7.3):
    1) title == wa_contacts.chat_title, MA solo se il title non e' ambiguo
       (>=2 contatti del tenant con lo stesso chat_title -> disabilitato
       per quel title).
    2) title parsabile come numero -> hmac -> wa_contacts.phone_hmac.
    3) nessun match -> (None, WaMatchedBy.none), diagnostica.

    hmac_phone si aspetta SEMPRE il numero normalizzato CON il '+'
    ricomposto (contratto di wa_ingest.py, M2: normalize_e164 ritorna le
    cifre senza '+', il '+' si riaggiunge subito prima di hmac_phone/
    encrypt -- mai l'output nudo di normalize_e164). Un title che supera
    il check title_is_number del POM (solo cifre/spazi/+) ma fallisce
    comunque normalize_e164 (lunghezza fuori range E.164) e' trattato come
    nessun match, non un errore: e' un titolo che sembra un numero ma non
    lo e' davvero."""
    if row.title_is_number:
        try:
            cifre = normalize_e164(row.title, default_country=settings.wa_ingest_default_country)
        except PhoneNormalizationError:
            return None, WaMatchedBy.none
        contatto = await db.scalar(
            select(WaContact).where(
                WaContact.tenant_id == tenant_id,
                WaContact.phone_hmac == hmac_phone("+" + cifre),
            )
        )
        if contatto is not None:
            return contatto, WaMatchedBy.phone
        return None, WaMatchedBy.none

    conteggio = await db.scalar(
        select(func.count(WaContact.id)).where(
            WaContact.tenant_id == tenant_id,
            WaContact.chat_title == row.title,
        )
    )
    if conteggio == 1:
        contatto = await db.scalar(
            select(WaContact).where(
                WaContact.tenant_id == tenant_id,
                WaContact.chat_title == row.title,
            )
        )
        return contatto, WaMatchedBy.chat_title

    return None, WaMatchedBy.none


async def _ultima_preview_vista(db, contact_id: str) -> str | None:
    ultimo = await db.scalar(
        select(WaInboundEvent.preview_text)
        .where(WaInboundEvent.contact_id == contact_id)
        .order_by(WaInboundEvent.detected_at.desc())
        .limit(1)
    )
    return ultimo


async def _incrementa_contatore_campagna(db, campaign_id: str, campo: str) -> None:
    """UPDATE ... SET x = x + 1 in SQL (contratto §4.2), stesso pattern di
    wa_sender._incrementa_contatore_campagna -- non importato da li' per non
    accoppiare i due moduli a una funzione privata dell'altro."""
    from sqlalchemy import update
    from app.models.wa import WaCampaign
    colonna = getattr(WaCampaign, campo)
    await db.execute(update(WaCampaign).where(WaCampaign.id == campaign_id)
                     .values({campo: colonna + 1}))


async def _campagna_attiva_del_contatto(db, contact_id: str) -> WaCampaignContact | None:
    """La riga wa_campaign_contacts NON terminale del contatto, se c'e' --
    usata sia per l'evento opt-out (campaign_id per il log) sia per la
    transizione a replied."""
    return await db.scalar(
        select(WaCampaignContact).where(
            WaCampaignContact.contact_id == contact_id,
            WaCampaignContact.status == WaContactStatus.in_sequence,
        )
    )


async def process_chat_row(db, *, tenant_id: str, wa_number_id: str, row: ChatRow) -> dict:
    """Un giro completo per una riga della lista chat con unread>0:
    match -> dedup -> opt-out o replied. Mai apre la chat (il chiamante
    passa gia' righe raccolte da scan_chat_list, che non apre nulla)."""
    contatto, matched_by = await match_contact(db, tenant_id, row)

    if contatto is None:
        db.add(WaInboundEvent(tenant_id=tenant_id, wa_number_id=wa_number_id,
                              contact_id=None, preview_text=row.preview,
                              matched_by=WaMatchedBy.none, processed=True))
        await db.commit()
        return {"esito": "non_associato", "contact_id": None}

    if await _ultima_preview_vista(db, contatto.id) == row.preview:
        return {"esito": "duplicato", "contact_id": contatto.id}

    if wa_optout.looks_like_stop(row.preview):
        cc_attiva = await _campagna_attiva_del_contatto(db, contatto.id)
        await wa_optout.persist_wa_optout(
            db, contatto.id, prova=row.preview,
            campaign_id=cc_attiva.campaign_id if cc_attiva else None)
        db.add(WaInboundEvent(tenant_id=tenant_id, wa_number_id=wa_number_id,
                              contact_id=contatto.id, preview_text=row.preview,
                              matched_by=matched_by, processed=True))
        await db.commit()
        return {"esito": "optout", "contact_id": contatto.id}

    cc_attiva = await _campagna_attiva_del_contatto(db, contatto.id)
    if cc_attiva is not None:
        cc_attiva.status = WaContactStatus.replied
        cc_attiva.replied_at_step = cc_attiva.current_step
        cc_attiva.next_action_at = None
        contatto.last_replied_at = datetime.utcnow()
        await _incrementa_contatore_campagna(db, cc_attiva.campaign_id, "replied")
        db.add(WaInboundEvent(tenant_id=tenant_id, wa_number_id=wa_number_id,
                              contact_id=contatto.id, preview_text=row.preview,
                              matched_by=matched_by, processed=True))
        await db.commit()
        events.emit(cc_attiva.campaign_id, "wa.reply.received",
                    f"contatto {contatto.id[:8]}: risposta rilevata dalla lista chat",
                    level="info")
        return {"esito": "replied", "contact_id": contatto.id}

    db.add(WaInboundEvent(tenant_id=tenant_id, wa_number_id=wa_number_id,
                          contact_id=contatto.id, preview_text=row.preview,
                          matched_by=matched_by, processed=True))
    await db.commit()
    return {"esito": "ignorato", "contact_id": contatto.id}


async def numeri_da_scansionare(db) -> list[str]:
    """Solo numeri attivi con almeno una campagna running che ha ancora
    contatti queued/in_sequence -- non serve scansionare un numero senza
    lavoro vivo (SDD §7.3: "solo numeri con campagne attive")."""
    righe = await db.execute(
        select(WaNumber.id)
        .join(WaCampaign, WaCampaign.wa_number_id == WaNumber.id)
        .join(WaCampaignContact, WaCampaignContact.campaign_id == WaCampaign.id)
        .where(
            WaNumber.status == WaNumberStatus.active,
            WaCampaign.status == WaCampaignStatus.running,
            WaCampaignContact.status.in_([WaContactStatus.queued,
                                          WaContactStatus.in_sequence]),
        )
        .distinct()
    )
    return [r[0] for r in righe.all()]
