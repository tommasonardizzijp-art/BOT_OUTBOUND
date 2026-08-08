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


def _pattern(parole_csv: str) -> re.Pattern:
    """Parole/frasi intere, case-insensitive. \\b su entrambi i lati: senza,
    'stopper' verrebbe letto come uno STOP e un cliente perderebbe un
    contatto per una parola qualsiasi."""
    parole = [p.strip() for p in (parole_csv or "").split(",") if p.strip()]
    if not parole:
        return re.compile(r"(?!x)x")  # non matcha mai: lista vuota = nessun STOP
    alternative = "|".join(re.escape(p) for p in parole)
    return re.compile(rf"\b({alternative})\b", re.IGNORECASE)


_SOGLIA_PAROLE_AMBIGUA = 3


def looks_like_stop(text) -> bool:
    """True se il testo e' un opt-out da trattare come immediato: una parola
    DURA in qualunque punto del messaggio, oppure una parola AMBIGUA (es.
    'basta') quando il messaggio e' sostanzialmente quella parola sola
    (testo normalizzato <= 3 parole). Un'ambigua dentro un messaggio piu'
    lungo ('mi basta sapere se siete aperti') NON e' opt-out qui -- vedi
    looks_like_ambiguous_stop_needs_review (review G6, 07/08: un falso
    opt-out e' un cliente perso per sempre, chi vuole uscire davvero puo'
    sempre ripetere con una parola dura, e' la CTA che gli mandiamo noi).

    Non solleva MAI: finisce dentro una guardia di sicurezza, e un'eccezione
    qui trasformerebbe un controllo in un crash intermittente."""
    if not isinstance(text, str) or not text.strip():
        return False
    try:
        if _pattern(settings.wa_stop_words).search(text):
            return True
        if _pattern(settings.wa_stop_words_ambigue).search(text):
            return len(text.strip().split()) <= _SOGLIA_PAROLE_AMBIGUA
        return False
    except Exception as exc:  # pragma: no cover - difesa, non logica
        logger.error(f"looks_like_stop: pattern non valido ({exc}) -- "
                     "trattato come NESSUNO stop, il chiamante ha la sentinella")
        return False


def looks_like_ambiguous_stop_needs_review(text) -> bool:
    """True se il testo contiene una parola AMBIGUA ma il messaggio e'
    troppo lungo per l'opt-out automatico di looks_like_stop -- non si marca
    nulla, ma qualcuno deve leggerlo a mano (il costo di ignorarlo del tutto
    e' lo stesso identico falso-opt-out che questa funzione esiste per
    evitare, solo spostato da 'automatico e silenzioso' a 'mai visto da
    nessuno'). Stessa garanzia di non-sollevare di looks_like_stop."""
    if not isinstance(text, str) or not text.strip():
        return False
    try:
        if not _pattern(settings.wa_stop_words_ambigue).search(text):
            return False
        return len(text.strip().split()) > _SOGLIA_PAROLE_AMBIGUA
    except Exception as exc:  # pragma: no cover - difesa, non logica
        logger.error(f"looks_like_ambiguous_stop_needs_review: pattern non "
                     f"valido ({exc})")
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


async def check_optout_circuit_breaker(db, campaign_id: str | None) -> bool:
    """Circuit breaker sul tasso di opt-out (review P4, 07/08): il numero
    che rischia il ban e' del CLIENTE, non nostro, e prima di questo fix
    nessuna soglia fermava nulla in automatico. Va chiamato DOPO ogni
    incremento del contatore opted_out di una campagna.

    Sotto wa_optout_breaker_min_invii il campione e' troppo piccolo per
    significare qualcosa (resta muto, non fa nulla). Sopra la soglia
    (wa_optout_breaker_pct) ferma l'INTERO canale con lo stesso kill-switch
    di POST /wa/ops/halt (bot_state_service.halt_wa) -- non solo la
    campagna: un tasso di opt-out alto e' un segnale sul NUMERO (template
    sbagliato, lista sporca, numero gia' segnalato), non sulla singola riga
    di configurazione.

    Idempotente per l'alert: se il canale e' gia' fermo (da questo breaker
    o da un halt manuale) non manda un secondo Telegram identico a ogni
    opt-out successivo -- halt_wa stesso resta innocuo da richiamare piu'
    volte (riscrive solo reason/timestamp), ma lo spam Telegram no."""
    from app.services import bot_state_service, notifier
    from app.models.wa import WaCampaign

    if not campaign_id:
        return False

    campagna = await db.scalar(select(WaCampaign).where(WaCampaign.id == campaign_id))
    if campagna is None:
        return False

    inviati = campagna.sent or 0
    if inviati < settings.wa_optout_breaker_min_invii:
        return False

    tasso = 100.0 * (campagna.opted_out or 0) / inviati
    if tasso <= settings.wa_optout_breaker_pct:
        return False

    if await bot_state_service.is_wa_halted(db):
        return False

    motivo = (f"circuit breaker opt-out: campagna {campaign_id[:8]} al "
              f"{tasso:.1f}% ({campagna.opted_out}/{inviati} invii), soglia "
              f"{settings.wa_optout_breaker_pct}%")
    await bot_state_service.halt_wa(reason=motivo, by="circuit_breaker", db=db)
    # Il commit e' QUI e non e' un dettaglio. halt_wa committa solo se ha
    # aperto lui la sessione (pattern own_session): con una sessione altrui
    # lascia la riga sporca e il commit tocca al chiamante. I due chiamanti
    # in wa_sender fanno `return` subito dopo, e sopra di loro il worker
    # tiene una sessione per messaggio dentro un `async with`: uscire dal
    # blocco CHIUDE la sessione, non la committa. Senza questa riga lo stop
    # non arrivava mai a DB -- partiva il Telegram "FERMATO in automatico" e
    # il canale continuava a inviare, perche' il worker rilegge
    # `is_wa_halted()` su una sessione nuova.
    #
    # Committare qui e' sicuro: a questo punto persist_wa_optout e
    # _incrementa_contatore_campagna hanno gia' committato per conto loro,
    # quindi non c'e' lavoro altrui in sospeso che questo commit anticipi.
    # E viene PRIMA dell'alert: mai annunciare uno stop che non e' salvato.
    await db.commit()
    try:
        await notifier.send_telegram(
            f"WhatsApp FERMATO in automatico -- {motivo}. Verifica prima di "
            "riattivare (POST /wa/ops/resume): puo' essere un template che "
            "sta convincendo la gente a uscire, non solo rumore statistico.",
            level="error")
    except Exception as exc:
        logger.error(f"[WA] alert circuit breaker non inviato: {type(exc).__name__}")
    return True
