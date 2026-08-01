"""Worker di invio del canale WhatsApp: mini-sessioni per-numero.

Calco dichiarato: services/browser_bio.py (claim atomico, Retry(defer) a
fine sessione, escalation su fallimenti consecutivi), applicato a
wa_campaign_contacts invece che a Follower. Le differenze rispetto a quel
file sono tutte commentate: dove non c'e' commento, e' lo stesso pattern.
"""
import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import or_, select, update

from app.browser.whatsapp_page import WhatsAppWebPage
from app.config import settings
from app.services import wa_sender, wa_timing
from app.services.wa_session import WHATSAPP_WEB_URL, _open_wa_browser

# Quanti guasti NOSTRI consecutivi (selettori, pagina in stato inatteso) su
# chat diverse fermano il numero. Tre: sotto si rischia di fermarsi per un
# blip di rete, sopra si insiste su un DOM rotto sprecando la lista.
# Contratto §3.2.
MAX_GUASTI_CONSECUTIVI = 3


async def claim_next_wa_contact(db, *, number_id: str, worker_id: str):
    """Prende UNA riga pronta per questo numero e la marca sotto lock.
    Ritorna (cc, contact, campaign, step) oppure None.

    La SELECT e' la query di eleggibilita' del contratto sez. 7.3: se cambia
    qui, cambia il contratto -- non e' un dettaglio di implementazione, e'
    l'interfaccia su cui M2 costruisce le proprie righe.
    """
    from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                               WaContact, WaContactStatus, WaNumber, WaNumberStatus,
                               WaSequenceStep)

    now = datetime.utcnow()
    stale_cutoff = now - timedelta(minutes=int(settings.wa_lock_timeout_min))

    riga = (
        select(WaCampaignContact, WaContact, WaCampaign)
        .join(WaCampaign, WaCampaign.id == WaCampaignContact.campaign_id)
        .join(WaContact, WaContact.id == WaCampaignContact.contact_id)
        .join(WaNumber, WaNumber.id == WaCampaign.wa_number_id)
        .where(
            WaCampaign.status == WaCampaignStatus.running,
            WaNumber.status == WaNumberStatus.active,
            WaNumber.id == number_id,
            WaCampaignContact.status.in_([WaContactStatus.queued,
                                          WaContactStatus.in_sequence]),
            WaCampaignContact.next_action_at.is_not(None),
            WaCampaignContact.next_action_at <= now,
            or_(WaCampaignContact.locked_by.is_(None),
                WaCampaignContact.locked_at < stale_cutoff),
            WaCampaignContact.failure_count < int(settings.wa_max_failures_per_contact),
            WaContact.opted_out.is_(False),
            WaContact.do_not_contact.is_(False),
        )
        .order_by(WaCampaignContact.next_action_at)
        .limit(1)
    )
    result = (await db.execute(riga)).first()
    if result is None:
        return None
    cc, contact, campaign = result

    # Claim atomico: la WHERE ripete la condizione di lock. Se un altro
    # worker ha vinto la corsa fra SELECT e UPDATE, rowcount e' 0 e qui si
    # esce senza errore -- stesso pattern di browser_bio.claim_next_pending.
    claim = await db.execute(
        update(WaCampaignContact)
        .where(
            WaCampaignContact.id == cc.id,
            or_(WaCampaignContact.locked_by.is_(None),
                WaCampaignContact.locked_at < stale_cutoff),
        )
        .values(locked_by=worker_id, locked_at=now)
    )
    await db.commit()
    if (claim.rowcount or 0) == 0:
        logger.debug(f"claim perso su {cc.id} (un altro worker e' arrivato prima)")
        return None

    step = await db.scalar(
        select(WaSequenceStep).where(
            WaSequenceStep.campaign_id == campaign.id,
            WaSequenceStep.step_index == (cc.current_step or -1) + 1,
        )
    )
    if step is None:
        # Contatto senza step successivo: non e' lavoro, e' una riga da
        # chiudere. Si rilascia il lock e si lascia al chiamante decidere.
        await db.execute(update(WaCampaignContact).where(WaCampaignContact.id == cc.id)
                         .values(locked_by=None, locked_at=None,
                                 status=WaContactStatus.completed, next_action_at=None))
        await db.commit()
        return None

    await db.refresh(cc)
    return cc, contact, campaign, step


def _ora_locale_corrente() -> int:
    """Ora locale del tenant. Fuso italiano fisso in MVP (SDD Q6: solo
    italiano, finestra oraria Europe/Rome); quando arrivera' il multi-lingua,
    questo diventa un campo del tenant, non una costante.

    Fallback a UTC+1 (CET) se il tz database non e' disponibile: stesso
    problema gia' documentato in manual_login._rome_utc_offset_seconds
    (zoneinfo su Windows puo' richiedere il pacchetto `tzdata`)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Rome")).hour
    except Exception:
        return datetime.now(timezone(timedelta(hours=1))).hour


async def esegui_mini_sessione(number_id: str) -> dict:
    """Una mini-sessione di invii per UN numero. Short-lived: apre il
    browser, manda al piu' N messaggi (wa_timing), chiude e lascia che sia
    il worker a rischedulare dopo il break. Mai sleep lunghi qui dentro.

    I quattro cancelli in AND (kill-switch, finestra oraria, cap numero, cap
    campagna) si ricontrollano ad OGNI messaggio con query live, mai con
    contatori stale (contratto §7.2). WA_SEND_ENABLED sta sopra tutti: a
    false non si apre nemmeno il browser.

    Il conteggio messaggi della sessione (wa_timing.wa_session_message_count)
    dipende dagli override per-campagna (session_min/max_messages), quindi si
    calcola SOLO dopo il primo claim riuscito, sulla campagna vera restituita
    da quel claim -- non prima, e non su un placeholder.

    Ritorna un dizionario di contatori: e' quello che il task ARQ logga e
    che i test leggono.
    """
    from app.database import AsyncSessionLocal
    from app.models.wa import WaNumber, WaNumberStatus
    from app.services import bot_state_service, wa_number_manager

    esito = {"inviati": 0, "falliti": 0, "saltati": 0, "motivo": "completata"}
    worker_id = f"wa-{number_id[:8]}-{uuid.uuid4().hex[:6]}"

    # Cancello 0: il master switch. A false non si apre nemmeno il browser.
    if not settings.wa_send_enabled:
        esito["motivo"] = "send_disabled"
        return esito

    # Cancello 1: kill-switch di canale (query live, non cache).
    if await bot_state_service.is_wa_halted():
        esito["motivo"] = "wa_halted"
        return esito

    async with AsyncSessionLocal() as db:
        number = await db.scalar(select(WaNumber).where(WaNumber.id == number_id))
        if number is None or number.status != WaNumberStatus.active:
            esito["motivo"] = "numero_non_attivo"
            return esito
        proxy_url = number.proxy_url
        if not proxy_url:
            # T3 della SDD: numeri diversi che escono dallo stesso IP
            # risultano correlati. Non blocca (in test non c'e' proxy), ma
            # deve essere rumoroso: il warning non compra i proxy, pero'
            # rende impossibile dire "non lo sapevamo".
            logger.warning(f"[WA] numero {number_id} senza proxy: rischio T3 "
                           "(correlazione multi-numero sullo stesso IP)")

    quanti = None          # calcolato dopo il primo claim, sulla campagna vera
    processati = 0
    guasti_consecutivi = 0

    async with _open_wa_browser(number_id, headless=True, proxy_url=proxy_url) as context:
        page = await context.new_page()
        await page.goto(WHATSAPP_WEB_URL, wait_until="domcontentloaded")
        pom = WhatsAppWebPage(page)
        browser_t0 = time.perf_counter()

        while quanti is None or processati < quanti:
            # I cancelli si ricontrollano a OGNI messaggio, non una volta a
            # inizio sessione: una sessione dura decine di minuti e nel
            # frattempo puo' cambiare tutto (kill-switch, cap, ora).
            if await bot_state_service.is_wa_halted():
                esito["motivo"] = "wa_halted"
                break

            async with AsyncSessionLocal() as db:
                number = await db.scalar(select(WaNumber).where(WaNumber.id == number_id))
                preso = await claim_next_wa_contact(db, number_id=number_id,
                                                    worker_id=worker_id)
                if preso is None:
                    esito["motivo"] = "niente_da_fare"
                    break
                cc, contact, campaign, step = preso

                if quanti is None:
                    quanti = wa_timing.wa_session_message_count(campaign)

                ora = _ora_locale_corrente()
                inizio, fine = wa_timing.effective_wa_active_hours(campaign)
                if not (inizio <= ora < fine):
                    await _rilascia_lock(db, cc)
                    esito["motivo"] = "fuori_finestra"
                    break

                if not await wa_number_manager.has_wa_send_budget(db, number, campaign):
                    await _rilascia_lock(db, cc)
                    esito["motivo"] = "cap_esaurito"
                    break

                res = await wa_sender.invia_a_contatto(
                    db, pom, campaign=campaign, step=step, cc=cc, contact=contact,
                    number=number,
                    browser_avviato_da_s=time.perf_counter() - browser_t0)
                processati += 1

                if res.stato == "sent":
                    esito["inviati"] += 1
                    guasti_consecutivi = 0
                elif res.stato in ("skipped", "opted_out", "replied"):
                    esito["saltati"] += 1
                    guasti_consecutivi = 0
                elif res.stato == "failed":
                    esito["falliti"] += 1
                    guasti_consecutivi = 0
                else:  # 'queued' = guasto nostro, il contatto non si tocca
                    await _rilascia_lock(db, cc)
                    guasti_consecutivi += 1

                if guasti_consecutivi >= MAX_GUASTI_CONSECUTIVI:
                    await _ferma_numero_per_guasto(db, number_id, campaign.id,
                                                   guasti_consecutivi)
                    esito["motivo"] = "guasti_consecutivi"
                    break

            # Delay lognormale FRA i messaggi, dentro la sessione. Non e' un
            # "sleep lungo": e' la mediana di 90s che rende il ritmo umano.
            await asyncio.sleep(wa_timing.wa_send_delay_seconds())

    logger.info(f"[WA] mini-sessione {number_id}: {esito}")
    return esito


async def _rilascia_lock(db, cc) -> None:
    from app.models.wa import WaCampaignContact
    await db.execute(update(WaCampaignContact).where(WaCampaignContact.id == cc.id)
                     .values(locked_by=None, locked_at=None))
    await db.commit()


async def _ferma_numero_per_guasto(db, number_id: str, campaign_id: str, n: int) -> None:
    """FM2: N fallimenti nostri consecutivi su chat diverse = DOM cambiato o
    pagina in stato inatteso. Si ferma il numero e si mette la campagna in
    error; i contatti restano queued perche' NON e' colpa loro. Un selettore
    rotto non deve bruciare una lista (SDD 11)."""
    from app.models.wa import WaCampaign, WaCampaignStatus, WaNumber, WaNumberStatus
    from app.services import notifier
    from app.utils import events

    await db.execute(update(WaNumber).where(WaNumber.id == number_id)
                     .values(status=WaNumberStatus.cooldown))
    await db.execute(update(WaCampaign).where(WaCampaign.id == campaign_id)
                     .values(status=WaCampaignStatus.error))
    await db.commit()
    events.emit(campaign_id, "wa.number.stopped",
                f"{n} guasti consecutivi: numero fermato, contatti intatti",
                level="error")
    await notifier.send_telegram(
        f"WhatsApp: numero fermato dopo {n} guasti consecutivi "
        f"(probabile DOM cambiato). Campagna in error, contatti NON bruciati.",
        level="error")
