"""Invio di UN messaggio WhatsApp: apertura chat, guardie, invio.

Il POM (whatsapp_page.py) espone segnali e non decide; la politica sta
qui. Ogni funzione di questo modulo che decide "si invia / non si invia"
e' pura o quasi, perche' deve essere provabile senza browser: le tre volte
in cui M1 ha sbagliato una guardia, il difetto era nel giudizio, non nel
DOM.
"""
from dataclasses import dataclass
from datetime import datetime

from loguru import logger

from app.browser.whatsapp_page import OpenResult

# Segnali del POM che significano "la chat 1:1 non esiste": colpa del dato,
# non nostra. Copiati alla lettera da whatsapp_page.open_chat /
# _apri_chat_da_risultati / _history_signal: se cambiano li', questo modulo
# smette di riconoscerli e cade nel ramo fail-closed (colpa nostra), che e'
# il fallimento giusto.
_SEGNALI_CHAT_INESISTENTE = (
    "nessuna-cronologia:nessun-messaggio-nel-pannello",
    "nessuna-cronologia:sezione-chat-vuota:nessuna-conversazione-esistente",
    "nessuna-cronologia:nessuna-sezione-chat:solo-gruppi-o-contatti-senza-conversazione",
)

# Segnali che dicono "la pagina non era nello stato che ci aspettavamo":
# infrastruttura nostra. Il contatto NON si tocca.
_SEGNALI_COLPA_NOSTRA = (
    "nessuna-cronologia:casella-ricerca-non-trovata",
    "nessuna-cronologia:ricerca-non-svuotata",
    "nessuna-cronologia:focus-non-sulla-ricerca-pre-invio",
)

_SEGNALE_RICERCA_VUOTA = "nessuna-cronologia:nessun-risultato-di-ricerca"


@dataclass
class EsitoApertura:
    puo_inviare: bool
    esito_contatto: str | None   # 'skipped' | None (None = non si tocca lo stato)
    motivo: str
    colpa_nostra: bool           # True -> conta verso l'escalation FM2 del numero


def valuta_apertura(res: OpenResult) -> EsitoApertura:
    """Traduce (ok, signal) del POM nella decisione. Contratto sez. 3.1-3.2.

    Fail-closed su tutto cio' che non e' riconosciuto: un segnale nuovo
    (POM aggiornato, WhatsApp cambiato) non deve mai finire nel ramo che
    marca il contatto, perche' quello e' irreversibile per il contatto e
    invisibile a chi guarda i log.
    """
    signal = res.signal or ""

    if res.ok and signal.startswith("cronologia:"):
        try:
            n = int(signal.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            logger.error(f"valuta_apertura: conteggio illeggibile in {signal!r} -- "
                         "non si invia")
            return EsitoApertura(False, None, "segnale_illeggibile", True)
        if n >= 1:
            return EsitoApertura(True, None, f"cronologia:{n}", False)
        return EsitoApertura(False, None, "cronologia_vuota", True)

    if signal in _SEGNALI_CHAT_INESISTENTE:
        return EsitoApertura(False, "skipped", "no_existing_chat", False)

    if signal in _SEGNALI_COLPA_NOSTRA:
        return EsitoApertura(False, None, signal.split(":", 1)[1], True)

    if signal == _SEGNALE_RICERCA_VUOTA:
        # Ambiguo: lo scioglie il chiamante col contesto di sessione (sez. 3.3).
        return EsitoApertura(False, None, "ricerca_senza_risultati", False)

    logger.error(f"valuta_apertura: segnale non catalogato {signal!r} -- "
                 "trattato come guasto nostro, il contatto non si tocca")
    return EsitoApertura(False, None, "segnale_non_catalogato", True)


@dataclass
class EsitoGuardia:
    puo_inviare: bool
    motivo: str
    prova: str | None = None      # il testo dell'inbound che ha bloccato


async def guardia_pre_invio(pom, *, gia_scritto_prima: bool,
                            browser_avviato_da_s: float) -> EsitoGuardia:
    """Guardia opt-out/reply a chat APERTA. E' la garanzia strutturale del
    canale (SDD 7.2): con questa, uno STOP non e' mai scavalcabile, anche
    tra campagne distanti mesi e anche se lo scan lista lo ha perso.

    Costo misurato in M0: mediana 5,7 s, p95 7,5 s, max 12,1 s -- di cui la
    quasi totalita' e' il caricamento della cronologia, che e' PARTE della
    guardia e non e' aggirabile (la conversazione e' virtualizzata).

    Ordine dei controlli scelto per costo crescente: prima quelli che non
    toccano il DOM.
    """
    from app.config import settings
    from app.services import wa_optout

    # 1. Quarantena post-riconnessione (contratto sez. 3.4.2). Costo zero, e
    #    copre la finestra in cui QUALUNQUE lettura sarebbe inaffidabile.
    #    ATTENZIONE: 15 minuti e' un valore STIMATO, non misurato -- si
    #    rimisura quando il selettore SYNC_INDICATOR verra' catturato.
    quarantena_s = float(settings.wa_resync_quarantine_min) * 60
    if browser_avviato_da_s < quarantena_s:
        return EsitoGuardia(False, "quarantena_risync")

    # 2. Indicatore di sincronizzazione. Oggi torna sempre 'unknown' e
    #    'unknown' NON vale 'synced': semplicemente non e' un segnale.
    #    'syncing' invece blocca -- e blocchera' da solo il giorno in cui il
    #    selettore sara' catalogato, senza toccare questo codice.
    if await pom.sync_state() == "syncing":
        return EsitoGuardia(False, "sincronizzazione_in_corso")

    # 3. Caricare la cronologia FA PARTE della guardia: senza, nel DOM
    #    restano ~17 messaggi degli ultimi minuti e uno STOP di venti minuti
    #    prima non esiste (misurato in M0).
    info = await pom.load_history(minimo=int(settings.wa_guard_history_min))

    # 3b. Scroll mai tentato (box del pannello non trovato): 'after' e' solo
    #     cio' che c'era gia' nel DOM prima della chiamata, non uno storico
    #     validato -- stesso principio del punto 5, un segnale illeggibile
    #     non e' un segnale sicuro.
    if not info.ok:
        return EsitoGuardia(False, "storico_non_caricato")

    # 4. Incoerenza DB<->DOM: se avevamo gia' scritto a questo contatto e il
    #    DOM mostra zero messaggi, la chat non e' sincronizzata. Vale una
    #    query e chiude la falla piu' pericolosa che ci resta aperta.
    if gia_scritto_prima and info.after == 0:
        return EsitoGuardia(False, "incoerenza_db_dom")

    # 5. Coda inbound. None = CECITA' (nessuna bolla agganciata, o righe
    #    malformate): non e' silenzio, e non si invia. [] = silenzio vero.
    coda = await pom.read_inbound_tail(n=int(settings.wa_guard_tail_n))
    if coda is None:
        return EsitoGuardia(False, "coda_non_agganciata")

    for testo in coda:
        if wa_optout.looks_like_stop(testo):
            return EsitoGuardia(False, "optout", prova=testo[:300])

    # Righe malformate (non stringa): il POM di oggi (_righe_ben_formate,
    # whatsapp_page.py) le filtra gia' a monte tornando None -- questo ramo
    # esiste in difesa di un POM futuro che non lo faccia piu'. E' colpa
    # NOSTRA (lettura rotta), non una verita' sul contatto: fail-closed su
    # un motivo dedicato che NON e' "ha_risposto", altrimenti il contatto
    # verrebbe marcato replied (terminale, irreversibile) per un nostro
    # difetto di lettura, e guasti_consecutivi si azzererebbe disarmando
    # l'escalation FM2 proprio quando il DOM e' meno affidabile.
    if any(not isinstance(t, str) for t in coda):
        return EsitoGuardia(False, "coda_malformata")

    # Una risposta qualsiasi ferma la sequenza (SDD 7.4, decisione 24/07),
    # ma NON e' questa funzione a marcarlo: qui si dice solo che c'e'.
    if coda:
        return EsitoGuardia(False, "ha_risposto", prova=coda[-1][:300])

    return EsitoGuardia(True, "silenzio")


def prepara_testo(step, contact, campaign) -> tuple[str, str]:
    """(testo pronto da digitare, variante 'a'..'d').

    Il rendering vero sta in wa_template.py, che e' di M2 (contratto sez. 2.4):
    qui si sceglie la variante, si rende, e si appende la CTA di opt-out --
    che il renderer NON deve conoscere, perche' e' una regola di campagna,
    non di template.
    """
    from app.services.wa_template import pick_wa_template, render_wa_template

    template, variante = pick_wa_template(step)
    testo = render_wa_template(
        template,
        display_name=getattr(contact, "display_name", None),
        attributes=getattr(contact, "attributes", None),
    )

    # CTA solo sul PRIMO messaggio della sequenza (SDD 7.2): ripeterla a
    # ogni step la trasforma in rumore, e l'obbligo ePrivacy riguarda il
    # primo contatto della campagna.
    if step.step_index == 0 and getattr(campaign, "optout_enabled", False):
        cta = (getattr(campaign, "optout_cta", None) or "").strip()
        if not cta:
            raise ValueError(
                "Campagna con optout_enabled=True e optout_cta vuota: non si "
                "manda marketing senza via d'uscita (contratto sez. 2.1)."
            )
        testo = f"{testo}\n\n{cta}"

    return testo, variante


@dataclass
class EsitoInvio:
    stato: str      # 'sent' | 'queued' | 'skipped' | 'failed' | 'opted_out' | 'replied'
    motivo: str


async def invia_a_contatto(db, pom, *, campaign, step, cc, contact, number,
                           browser_avviato_da_s: float) -> EsitoInvio:
    """Invia UN messaggio a UN contatto, con tutte le guardie. Non decide
    cap, finestra oraria o kill-switch: quelli li ha gia' verificati la
    mini-sessione (Task 11) prima di chiamare qui.

    Il numero in chiaro esiste solo dentro questa funzione, in memoria, il
    tempo di aprire la chat (P12): si decifra qui e non si logga mai --
    tutti i log usano mask_phone.
    """
    from sqlalchemy import func, select

    from app.config import settings
    from app.models.wa import WaContactStatus, WaMessage, WaMessageStatus
    from app.services import wa_number_manager, wa_optout
    from app.utils import events
    from app.utils.crypto import decrypt
    from app.utils.phone_pseudonym import mask_phone

    e164 = decrypt(contact.encrypted_phone)
    masked = mask_phone(e164)

    # --- apertura chat -----------------------------------------------------
    apertura = valuta_apertura(await pom.open_chat(e164))
    if not apertura.puo_inviare:
        logger.info(f"[WA] {masked}: apertura -> {apertura.motivo} "
                    f"(colpa_nostra={apertura.colpa_nostra})")
        if apertura.esito_contatto == "skipped":
            await _marca_contatto(db, cc, WaContactStatus.skipped,
                                  errore=apertura.motivo)
            return EsitoInvio("skipped", apertura.motivo)
        if apertura.colpa_nostra:
            return EsitoInvio("queued", apertura.motivo)
        # ambiguo (ricerca senza risultati): conta il fallimento, non brucia
        await _incrementa_fallimento(db, cc, apertura.motivo)
        return EsitoInvio("queued", apertura.motivo)

    # --- guardia pre-invio -------------------------------------------------
    gia_scritto = bool(await db.scalar(
        select(func.count(WaMessage.id)).where(
            WaMessage.contact_id == contact.id,
            WaMessage.status == WaMessageStatus.sent,
        )
    ))
    guardia = await guardia_pre_invio(pom, gia_scritto_prima=gia_scritto,
                                      browser_avviato_da_s=browser_avviato_da_s)
    if not guardia.puo_inviare:
        return await _esito_guardia_negativa(db, cc, contact, campaign, guardia, masked)

    # --- testo -------------------------------------------------------------
    try:
        testo, variante = prepara_testo(step, contact, campaign)
    except ValueError as exc:
        # prepara_testo solleva ValueError SOLO per optout_cta vuota con
        # optout_enabled=True: un errore di CONFIGURAZIONE della campagna,
        # identico per OGNI contatto -- colpa nostra (Fix 2, review finale
        # C2), non un problema di questo contatto. 'queued' arma FM2 invece
        # di bruciare la lista come DNC dopo 3 giri.
        logger.error(f"[WA] {masked}: config campagna invalida ({exc}) -- "
                     "il contatto non si tocca, il numero continua")
        return EsitoInvio("queued", "config_campagna")
    except Exception as exc:
        logger.error(f"[WA] {masked}: render fallito ({type(exc).__name__}) -- "
                     "il contatto va in failed, il numero continua")
        await _incrementa_fallimento(db, cc, f"render:{type(exc).__name__}")
        return EsitoInvio("failed", "render")

    msg = WaMessage(campaign_id=campaign.id, contact_id=contact.id,
                    wa_number_id=number.id, step_index=step.step_index,
                    template_variant=variante, rendered_text=testo,
                    status=WaMessageStatus.sending)
    db.add(msg)
    await db.commit()

    # --- RILETTURA TOCTOU: fra guardia e invio passano ~20s misurati -------
    # Non si ricarica la cronologia (gia' fatta dalla guardia): costa poco ed
    # e' l'unica difesa contro uno STOP arrivato nel frattempo.
    coda2 = await pom.read_inbound_tail(n=int(settings.wa_guard_tail_n))
    if coda2 is None:
        msg.status = WaMessageStatus.skipped
        msg.error = "coda_non_agganciata_seconda_lettura"
        await db.commit()
        return EsitoInvio("queued", "cecita_toctou")
    for testo_in in coda2:
        if wa_optout.looks_like_stop(testo_in):
            msg.status = WaMessageStatus.skipped
            msg.error = "stop_nella_finestra_toctou"
            await db.commit()
            await wa_optout.persist_wa_optout(db, contact.id, prova=testo_in,
                                              campaign_id=campaign.id)
            await _incrementa_contatore_campagna(db, campaign.id, "opted_out")
            logger.warning(f"[WA] {masked}: STOP arrivato nella finestra TOCTOU, "
                           "invio annullato")
            return EsitoInvio("opted_out", "stop_toctou")

    # --- invio -------------------------------------------------------------
    try:
        await pom.send_text(testo)
    except Exception as exc:
        # send_text che solleva = composer non trovato = DOM/selettore
        # cambiato = ESATTAMENTE FM2, colpa nostra (Fix 2, review finale
        # C2). Prima tornava 'failed' e _incrementa_fallimento marcava DNC
        # dopo 3 giri per un selettore rotto; 'queued' arma l'escalation
        # del numero (contratto §11) invece di bruciare la lista.
        msg.status = WaMessageStatus.failed
        msg.error = f"{type(exc).__name__}: {exc}"[:500]
        await db.commit()
        await _incrementa_contatore_campagna(db, campaign.id, "failed")
        logger.error(f"[WA] {masked}: invio fallito ({type(exc).__name__})")
        return EsitoInvio("queued", "send_text")

    tick = await pom.read_last_tick()
    msg.status = WaMessageStatus.sent
    msg.sent_at = datetime.utcnow()
    msg.delivery_check = _delivery_da_tick(tick)
    await db.commit()

    # chat_title si impara qui, ma SOLO se e' un nome: se e' un numero,
    # salvarlo metterebbe PII in chiaro a DB (P12, contratto §4.1).
    await _impara_chat_title(db, pom, contact)

    await wa_number_manager.record_wa_sent(db, number.id)
    await _incrementa_contatore_campagna(db, campaign.id, "sent")
    await _avanza_contatto(db, cc, campaign, step)
    contact.last_contacted_at = datetime.utcnow()
    await db.commit()

    events.emit(campaign.id, "wa.message.sent",
                f"inviato a {masked} (variante {variante}, spunta {tick})")
    logger.info(f"[WA] {masked}: inviato, spunta={tick}")
    return EsitoInvio("sent", "ok")


def _delivery_da_tick(tick: str):
    """La spunta e' testo LOCALIZZATO IN ITALIANO (SDD A4, Q39): un cliente
    con interfaccia in altra lingua la rompe. Non e' un gate -- e'
    best-effort e finisce solo in delivery_check."""
    from app.models.wa import WaDeliveryCheck
    t = (tick or "").lower()
    if "letto" in t:
        return WaDeliveryCheck.double_tick
    if "consegnato" in t:
        return WaDeliveryCheck.single_tick
    if "orolog" in t or "attesa" in t:
        return WaDeliveryCheck.clock
    return WaDeliveryCheck.none


async def _impara_chat_title(db, pom, contact) -> None:
    """Il titolo serve al watcher di M4 per agganciare le risposte. Si
    salva SOLO se e' un nome: title_is_number distingue i contatti non in
    rubrica (8 su 68 misurati in M0), e per quelli il matching usa gia'
    phone_hmac."""
    if contact.chat_title:
        return
    try:
        righe = await pom.scan_chat_list()
    except Exception as exc:
        logger.debug(f"chat_title non appreso ({type(exc).__name__}): non e' un errore")
        return
    if righe and not righe[0].title_is_number and righe[0].title:
        contact.chat_title = righe[0].title[:200]
        await db.commit()


async def _incrementa_contatore_campagna(db, campaign_id: str, campo: str) -> None:
    """UPDATE ... SET x = x + 1 in SQL (contratto §4.2). Mai leggere,
    sommare e riscrivere: con due worker si perdono conteggi in silenzio."""
    from sqlalchemy import update
    from app.models.wa import WaCampaign
    colonna = getattr(WaCampaign, campo)
    await db.execute(update(WaCampaign).where(WaCampaign.id == campaign_id)
                     .values({campo: colonna + 1}))
    await db.commit()


async def _marca_contatto(db, cc, stato, *, errore: str | None = None) -> None:
    cc.status = stato
    cc.last_error = errore
    cc.next_action_at = None
    cc.locked_by = None
    cc.locked_at = None
    await db.commit()


async def _incrementa_fallimento(db, cc, motivo: str) -> None:
    """failure_count + rinvio a 6 ore (contratto §3.3). Oltre soglia il
    contatto diventa non-raggiungibile: e' l'unica via per cui M3 scrive un
    DNC 'unreachable'.

    Il discriminatore del contratto §3.3 (2 tentativi in sessioni diverse +
    altri contatti aperti con successo, prima di concludere "non esiste su
    WhatsApp") NON e' implementato: questo resta un contatore semplice a
    soglia. Decisione di Tommaso (round1 post-review, non build-tonight):
    quando la soglia scatta specificamente per 'ricerca_senza_risultati'
    (l'unico percorso ambiguo -- gli altri motivi sono guasti nostri gia'
    accertati) si avvisa un umano via Telegram, cosi' puo' rivedere questi
    casi nel tempo e distinguere un bug reale da un vero non-raggiungibile."""
    from datetime import timedelta
    from app.config import settings
    from app.models.wa import WaContactStatus, WaDncReason
    from app.services import notifier
    from sqlalchemy import select
    from app.models.wa import WaContact

    cc.failure_count = (cc.failure_count or 0) + 1
    cc.last_error = motivo[:500]
    cc.next_action_at = datetime.utcnow() + timedelta(hours=6)
    dnc_ambiguo = False
    if cc.failure_count >= int(settings.wa_max_failures_per_contact):
        cc.status = WaContactStatus.skipped
        cc.next_action_at = None
        contact = await db.scalar(select(WaContact).where(WaContact.id == cc.contact_id))
        if contact is not None:
            contact.do_not_contact = True
            dnc_ambiguo = motivo == "ricerca_senza_risultati"
            contact.dnc_reason = (WaDncReason.invalid_number if dnc_ambiguo
                                  else WaDncReason.unreachable)
    await db.commit()

    if dnc_ambiguo:
        await notifier.send_telegram(
            f"WhatsApp: contatto {cc.contact_id[:8]} marcato non raggiungibile "
            f"dopo {cc.failure_count} ricerche senza risultati -- discriminatore "
            "§3.3 non implementato, verifica manuale consigliata (potrebbe essere "
            "un bug, non un vero non-raggiungibile).",
            level="warning")


async def _avanza_contatto(db, cc, campaign, step) -> None:
    """Dopo un invio riuscito: current_step avanza e il contatto si chiude
    se non ci sono altri step. In MVP c'e' solo lo step 0, quindi la strada
    normale e' 'completed' -- ma la query sullo step successivo e' gia'
    qui, cosi' M4 accende il multi-step senza toccare questa funzione."""
    from datetime import timedelta
    from sqlalchemy import select
    from app.models.wa import WaContactStatus, WaSequenceStep

    cc.current_step = step.step_index
    prossimo = await db.scalar(
        select(WaSequenceStep)
        .where(WaSequenceStep.campaign_id == campaign.id,
               WaSequenceStep.step_index == step.step_index + 1)
    )
    if prossimo is None:
        cc.status = WaContactStatus.completed
        cc.next_action_at = None
    else:
        cc.status = WaContactStatus.in_sequence
        cc.next_action_at = datetime.utcnow() + timedelta(days=int(prossimo.wait_days or 0))
    cc.locked_by = None
    cc.locked_at = None
    await db.commit()


async def _esito_guardia_negativa(db, cc, contact, campaign, guardia, masked: str) -> EsitoInvio:
    """Traduce l'esito della guardia in stato del contatto. Le tre uscite
    non sono equivalenti: 'optout' e 'ha_risposto' sono verita' sul
    contatto, tutto il resto e' un limite NOSTRO e lascia la riga queued."""
    from app.models.wa import WaContactStatus
    from app.services import wa_optout

    if guardia.motivo == "optout":
        await wa_optout.persist_wa_optout(db, contact.id, prova=guardia.prova or "",
                                          campaign_id=campaign.id)
        await _incrementa_contatore_campagna(db, campaign.id, "opted_out")
        logger.warning(f"[WA] {masked}: STOP in coda, invio annullato")
        return EsitoInvio("opted_out", "stop")

    if guardia.motivo == "ha_risposto":
        cc.status = WaContactStatus.replied
        cc.next_action_at = None
        cc.locked_by = None
        cc.locked_at = None
        await db.commit()
        logger.info(f"[WA] {masked}: ha gia' risposto, la sequenza si ferma qui")
        return EsitoInvio("replied", "ha_risposto")

    logger.warning(f"[WA] {masked}: guardia negativa ({guardia.motivo}) -- "
                   "il contatto resta queued, non e' colpa sua")
    return EsitoInvio("queued", guardia.motivo)
