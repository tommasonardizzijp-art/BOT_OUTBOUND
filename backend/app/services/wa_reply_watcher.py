"""Reply-watcher del canale WhatsApp (SDD §7.3): legge SOLO la lista chat
(sidebar), mai apre una conversazione -- aprirla marcherebbe "letto" e
brucerebbe le notifiche del cliente sul telefono (vincolo di coesistenza,
SDD §9). Matching contatto, dedup eventi, dispatch opt-out/replied.
"""
import unicodedata
from datetime import datetime

from loguru import logger
from sqlalchemy import func, select

from app.browser.whatsapp_page import ChatRow, WhatsAppWebPage
from app.config import settings
from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                           WaContact, WaContactStatus, WaInboundEvent,
                           WaMatchedBy, WaNumber, WaNumberStatus)
from app.services import bot_state_service, notifier, wa_optout, wa_profile_lock
from app.services.wa_session import WHATSAPP_WEB_URL, _open_wa_browser
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

    # NFC/NFD: il DOM puo' restituire lo stesso nome accentato in due forme
    # di normalizzazione Unicode diverse (backlog M4). Normalizziamo row.title
    # a NFC (standard raccomandato) e confrontiamo contro ENTRAMBE le forme:
    # non tutti i chat_title gia' a DB sono garantiti NFC (scritti prima di
    # questo fix), quindi un confronto solo-NFC lascerebbe indietro i dati
    # vecchi in NFD. Nessuna normalizzazione Unicode portabile lato SQL
    # (SQLite e Postgres non la offrono in modo uniforme): si confronta
    # contro il piccolo insieme di varianti possibili invece.
    titolo_nfc = unicodedata.normalize("NFC", row.title)
    titolo_nfd = unicodedata.normalize("NFD", titolo_nfc)
    varianti_titolo = {titolo_nfc, titolo_nfd}

    conteggio = await db.scalar(
        select(func.count(WaContact.id)).where(
            WaContact.tenant_id == tenant_id,
            WaContact.chat_title.in_(varianti_titolo),
        )
    )
    if conteggio == 1:
        contatto = await db.scalar(
            select(WaContact).where(
                WaContact.tenant_id == tenant_id,
                WaContact.chat_title.in_(varianti_titolo),
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


def _priorita_riga():
    """Ordine per stato e non per data: SDD Q2 ammette max 1 campagna running
    per numero, quindi in MVP le righe candidate sono realisticamente una
    sola. Se ce ne fossero piu' d'una (campagne diverse chiuse in momenti
    diversi), una sequenza ancora viva vale piu' di una gia' chiusa: e'
    quella che deve fermarsi."""
    from sqlalchemy import case

    return case(
        (WaCampaignContact.status == WaContactStatus.in_sequence, 0),
        (WaCampaignContact.status == WaContactStatus.queued, 1),
        else_=2,
    )


async def _campagna_attiva_del_contatto(db, contact_id: str) -> WaCampaignContact | None:
    """La riga wa_campaign_contacts cui attribuire un evento OPT-OUT, se c'e'
    (serve il campaign_id per il log, il contatore e il breaker).

    Include `queued` di proposito, e qui e' giusto: un opt-out non dipende
    dall'aver ricevuto qualcosa da noi. Se il cliente scrive STOP a un numero
    con cui era gia' in conversazione, quel STOP vale -- stringere anche questo
    ramo significherebbe mandare marketing a chi ha detto di no.

    Per la transizione a `replied` serve invece la funzione sotto: la' `queued`
    e' esattamente il caso da escludere."""
    return await db.scalar(
        select(WaCampaignContact)
        .where(
            WaCampaignContact.contact_id == contact_id,
            WaCampaignContact.status.in_([WaContactStatus.in_sequence,
                                          WaContactStatus.completed,
                                          WaContactStatus.queued]),
        )
        .order_by(_priorita_riga())
    )


async def _riga_da_marcare_replied(db, contact_id: str) -> WaCampaignContact | None:
    """La riga da portare a `replied`, se e solo se ha GIA' RICEVUTO il nostro
    messaggio.

    `completed` e' incluso, non solo `in_sequence`: le campagne MVP sono a un
    solo step (SDD Q29), quindi appena l'invio finisce il contatto passa a
    `completed` e non torna mai `in_sequence`. Cercando solo `in_sequence` il
    ramo replied era irraggiungibile in produzione -- nessun replied, nessun
    evento, nessun contatore proprio nello scenario normale (risposta che
    arriva ore o giorni dopo l'unico messaggio).

    `queued` invece NON c'e' piu', e serve dire perche' era stato incluso: il
    commento originale diceva che copriva "il caso simmetrico raro: risposta
    arrivata prima che partisse il nostro invio". Ma quel caso non e' una
    risposta, e `replied` e' TERMINALE: la riga esce dalla campagna e quel
    contatto non riceve mai il messaggio. Non e' una statistica sbagliata, e'
    un contatto scartato in silenzio senza essere mai stato contattato.
    Misurato dal vivo il 12/08 sulla campagna dei 246 di Primero: **2 righe su
    3** in `replied` avevano ZERO messaggi inviati -- conversazioni gia' in
    corso col cliente, lette come risposte. Su un numero con molte chat vive la
    perdita e' silenziosa e proporzionale al traffico legittimo.

    `current_step >= 0` e' il secondo cancello, e non e' ridondante con lo
    stato: `current_step` vale -1 all'arruolamento e passa a 0 col primo invio,
    quindi e' il marcatore diretto di "ha ricevuto". Una riga chiusa senza aver
    mai inviato (skip e poi chiusura) resta fuori anche se lo stato da solo la
    ammetterebbe."""
    return await db.scalar(
        select(WaCampaignContact)
        .where(
            WaCampaignContact.contact_id == contact_id,
            WaCampaignContact.status.in_([WaContactStatus.in_sequence,
                                          WaContactStatus.completed]),
            WaCampaignContact.current_step >= 0,
        )
        .order_by(_priorita_riga())
    )


async def process_chat_row(db, *, tenant_id: str, wa_number_id: str, row: ChatRow) -> dict:
    """Un giro completo per una riga della lista chat con unread>0:
    match -> dedup -> opt-out o replied. Mai apre la chat (il chiamante
    passa gia' righe raccolte da scan_chat_list, che non apre nulla)."""
    contatto, matched_by = await match_contact(db, tenant_id, row)

    if contatto is None:
        # Nessun preview_text per una riga non associata: su un numero in
        # coesistenza la sidebar contiene anche le chat PERSONALI del cliente,
        # e questa riga non alimenta nessuna decisione (niente opt-out, niente
        # replied) -- archiviarne il testo sarebbe conservare conversazioni di
        # terzi senza scopo. Per la diagnostica bastano il numero di righe e
        # matched_by=none.
        db.add(WaInboundEvent(tenant_id=tenant_id, wa_number_id=wa_number_id,
                              contact_id=None, preview_text=None,
                              matched_by=WaMatchedBy.none, processed=True))
        await db.commit()
        return {"esito": "non_associato", "contact_id": None}

    if await _ultima_preview_vista(db, contatto.id) == row.preview:
        return {"esito": "duplicato", "contact_id": contatto.id}

    if wa_optout.looks_like_stop(row.preview):
        gia_optato = bool(contatto.opted_out)
        cc_attiva = await _campagna_attiva_del_contatto(db, contatto.id)
        await wa_optout.persist_wa_optout(
            db, contatto.id, prova=row.preview,
            campaign_id=cc_attiva.campaign_id if cc_attiva else None)
        if cc_attiva is not None and not gia_optato:
            await _incrementa_contatore_campagna(db, cc_attiva.campaign_id, "opted_out")
            await wa_optout.check_optout_circuit_breaker(db, cc_attiva.campaign_id)
        db.add(WaInboundEvent(tenant_id=tenant_id, wa_number_id=wa_number_id,
                              contact_id=contatto.id, preview_text=row.preview,
                              matched_by=matched_by, processed=True))
        await db.commit()
        return {"esito": "optout", "contact_id": contatto.id}

    if wa_optout.looks_like_ambiguous_stop_needs_review(row.preview):
        # Parola ambigua (es. 'basta') in un messaggio troppo lungo per
        # l'opt-out automatico (review G6, 07/08): la sequenza si ferma
        # comunque sotto -- 'una risposta qualsiasi' -- ma qui serve un
        # umano che legga e decida se era davvero un opt-out. Best-effort:
        # un blip Telegram non deve impedire di fermare la sequenza sotto.
        try:
            await notifier.send_telegram(
                f"WhatsApp: contatto {contatto.id[:8]} ha scritto qualcosa con "
                f"'basta' ma non abbastanza corto per un opt-out automatico -- "
                f"verifica a mano se serve fermarlo: {row.preview[:200]!r}",
                level="warning")
        except Exception as exc:
            logger.error(f"[WA] alert 'basta' ambiguo non inviato: {type(exc).__name__}")

    cc_attiva = await _riga_da_marcare_replied(db, contatto.id)
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
    """Numeri attivi da scansionare, per due motivi indipendenti.

    (a) Lavoro ancora vivo: almeno una campagna running con contatti
        queued/in_sequence (SDD §7.3, "solo numeri con campagne attive").
    (b) Invio recente: almeno un wa_messages.sent_at negli ultimi
        wa_reply_scan_window_days giorni.

    Senza (b) il criterio (a) da solo spegneva la scansione proprio quando
    serve: le campagne MVP sono a un solo step (SDD Q29), a invio finito i
    contatti sono `completed` e il numero uscirebbe dalla lista mentre le
    risposte devono ancora arrivare. La finestra e' delimitata di proposito:
    includere i `completed` per sempre farebbe crescere la lista senza
    controllo."""
    from datetime import timedelta
    from app.models.wa import WaMessage

    con_lavoro = await db.execute(
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

    finestra = datetime.utcnow() - timedelta(days=int(settings.wa_reply_scan_window_days))
    con_invio_recente = await db.execute(
        select(WaMessage.wa_number_id)
        .join(WaNumber, WaNumber.id == WaMessage.wa_number_id)
        .where(
            WaNumber.status == WaNumberStatus.active,
            WaMessage.sent_at.is_not(None),
            WaMessage.sent_at >= finestra,
        )
        .distinct()
    )

    # dict.fromkeys: dedup mantenendo un ordine stabile fra le due query.
    return list(dict.fromkeys([r[0] for r in con_lavoro.all()]
                              + [r[0] for r in con_invio_recente.all()]))


async def scan_number(number_id: str) -> dict:
    """Una scansione della lista chat per UN numero: apre il browser sotto
    lucchetto profilo, legge SOLO la sidebar (mai una chat), processa ogni
    riga con unread>0. Short-lived, nessun sleep lungo."""
    from app.database import AsyncSessionLocal

    esito = {"scansionate": 0, "optout": 0, "replied": 0, "non_associati": 0, "motivo": None}

    if await bot_state_service.is_wa_halted():
        esito["motivo"] = "wa_halted"
        return esito

    async with AsyncSessionLocal() as db:
        numero = await db.scalar(select(WaNumber).where(WaNumber.id == number_id))
        if numero is None or numero.status != WaNumberStatus.active:
            esito["motivo"] = "numero_non_attivo"
            return esito
        tenant_id, proxy_url = numero.tenant_id, numero.proxy_url

    try:
        async with wa_profile_lock.held(number_id):
            async with _open_wa_browser(number_id, headless=True, proxy_url=proxy_url) as context:
                page = await context.new_page()
                await page.goto(WHATSAPP_WEB_URL, wait_until="domcontentloaded")
                pom = WhatsAppWebPage(page)
                # Trovato dal vivo (QA Fase 4, 04/08): subito dopo il goto la
                # sidebar non e' garantita pronta (SPA pesante). session_state()
                # (M1) ASPETTA fino a SESSION_STATE_TIMEOUT_CHATLIST_MS la
                # comparsa di CHATLIST; scan_chat_list() (M1) NON aspetta e
                # solleva RuntimeError se valutato troppo presto. Verificare
                # prima evita di scambiare un ritardo di caricamento per un
                # selettore disallineato.
                stato = await pom.session_state()
                if stato != "logged_in":
                    esito["motivo"] = "sessione_non_pronta"
                    return esito
                righe = await pom.scan_chat_list()
    except wa_profile_lock.WaProfileBusy:
        esito["motivo"] = "profilo_occupato"
        return esito

    async with AsyncSessionLocal() as db:
        for row in righe:
            # L'unread da solo NON e' il criterio giusto, e il 12/08 e' costato
            # un opt-out perso: la sessione di invio apre la chat per scrivere e
            # la lascia aperta uscendo, quindi uno STOP che arriva mentre quella
            # chat e' l'attiva a schermo viene marcato letto all'istante da
            # WhatsApp Web -- unread torna 0 e la riga spariva prima che qualcuno
            # ne guardasse la preview. Nel PoC il browser non inviava, non apriva
            # chat, e la condizione non si presentava mai.
            #
            # Il criterio corretto e' la DIREZIONE dell'ultimo messaggio. La
            # preview della sidebar c'e' anche per le chat lette e non serve
            # aprire nulla per leggerla: il vincolo di coesistenza (mai aprire
            # una conversazione) resta intatto.
            #
            # La seconda meta' della condizione non e' un dettaglio: su una chat
            # letta la preview e' l'ultimo messaggio CHIUNQUE l'abbia scritto. Se
            # e' il nostro, processare la riga marcherebbe `replied` -- che e'
            # terminale -- un contatto che non ha mai risposto.
            if row.unread_count <= 0 and row.last_is_outbound:
                continue
            esito["scansionate"] += 1
            risultato = await process_chat_row(db, tenant_id=tenant_id,
                                               wa_number_id=number_id, row=row)
            if risultato["esito"] == "optout":
                esito["optout"] += 1
            elif risultato["esito"] == "replied":
                esito["replied"] += 1
            elif risultato["esito"] == "non_associato":
                esito["non_associati"] += 1

    logger.info(f"[WA] reply-scan {number_id}: {esito}")
    return esito
