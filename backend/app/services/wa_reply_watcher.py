"""Reply-watcher del canale WhatsApp (SDD §7.3): legge SOLO la lista chat
(sidebar), mai apre una conversazione -- aprirla marcherebbe "letto" e
brucerebbe le notifiche del cliente sul telefono (vincolo di coesistenza,
SDD §9). Matching contatto, dedup eventi, dispatch opt-out/replied.
"""
from sqlalchemy import func, select

from app.browser.whatsapp_page import ChatRow
from app.config import settings
from app.models.wa import WaContact, WaMatchedBy
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
