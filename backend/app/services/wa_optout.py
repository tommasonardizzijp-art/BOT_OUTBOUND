"""Opt-out del canale WhatsApp: riconoscimento dello STOP e persistenza del
tag DNC permanente per-tenant (SDD 7.5, decisione 24/07).

Due funzioni separate di proposito: `looks_like_stop` e' pura e testabile
senza DB (e' il giudizio), `persist_wa_optout` e' la scrittura. La guardia
pre-invio (wa_sender) usa entrambe; il watcher di M4 usera' le stesse.

"Visto una volta, vale per sempre": il DOM puo' smettere di mostrare lo
STOP (cronologia non sincronizzata, chat archiviata, messaggio cancellato),
la decisione no. Per questo la prova si scrive a DB e non si ricalcola.
"""
import re
from datetime import datetime

from loguru import logger
from sqlalchemy import select, update

from app.config import settings
from app.utils import events


def _stop_pattern() -> re.Pattern:
    """Parole/frasi intere, case-insensitive. \\b su entrambi i lati: senza,
    'stopper' verrebbe letto come uno STOP e un cliente perderebbe un
    contatto per una parola qualsiasi."""
    parole = [p.strip() for p in (settings.wa_stop_words or "").split(",") if p.strip()]
    if not parole:
        return re.compile(r"(?!x)x")  # non matcha mai: lista vuota = nessun STOP
    alternative = "|".join(re.escape(p) for p in parole)
    return re.compile(rf"\b({alternative})\b", re.IGNORECASE)


def looks_like_stop(text) -> bool:
    """True se il testo contiene una parola di opt-out. Non solleva MAI:
    finisce dentro una guardia di sicurezza, e un'eccezione qui
    trasformerebbe un controllo in un crash intermittente."""
    if not isinstance(text, str) or not text.strip():
        return False
    try:
        return bool(_stop_pattern().search(text))
    except Exception as exc:  # pragma: no cover - difesa, non logica
        logger.error(f"looks_like_stop: pattern non valido ({exc}) -- "
                     "trattato come NESSUNO stop, il chiamante ha la sentinella")
        return False


async def persist_wa_optout(db, contact_id: str, *, prova: str,
                            campaign_id: str | None = None) -> int:
    """Marca il contatto opted_out + do_not_contact (permanente, per-tenant)
    e porta a `opted_out` tutte le sue righe campagna NON terminali, di
    QUALUNQUE campagna del tenant. Ritorna quante righe ha fermato.

    `prova` e' il testo dell'inbound che ha fatto scattare l'opt-out: si
    salva come prova dell'opposizione (SDD 7.5 punto 7). Il numero non
    compare: la riga e' agganciata a contact_id, che e' gia' pseudonimo.

    Idempotente: un secondo STOP non ricalcola nulla e non ri-conta righe
    gia' terminali.
    """
    from app.models.wa import (WaCampaignContact, WaContact, WaContactStatus,
                               WaDncReason)

    contact = await db.scalar(select(WaContact).where(WaContact.id == contact_id))
    if contact is None:
        logger.error(f"persist_wa_optout: contatto {contact_id} inesistente")
        return 0

    gia_optato = bool(contact.opted_out)
    if not gia_optato:
        contact.opted_out = True
        contact.opted_out_at = datetime.utcnow()
        contact.do_not_contact = True
        contact.dnc_reason = WaDncReason.optout

    terminali = (WaContactStatus.opted_out, WaContactStatus.completed,
                 WaContactStatus.skipped, WaContactStatus.replied)
    result = await db.execute(
        update(WaCampaignContact)
        .where(
            WaCampaignContact.contact_id == contact_id,
            WaCampaignContact.status.notin_(terminali),
        )
        .values(status=WaContactStatus.opted_out, next_action_at=None,
                locked_by=None, locked_at=None)
    )
    fermate = result.rowcount or 0
    await db.commit()

    logger.warning(
        f"[WA OPTOUT] contatto={contact_id} righe_fermate={fermate} "
        f"prova={prova[:60]!r}"
    )
    if campaign_id:
        events.emit(campaign_id, "wa.optout",
                    f"contatto {contact_id}: STOP rilevato, {fermate} sequenze fermate",
                    level="warning")
    return fermate
