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

import arq
from loguru import logger
from sqlalchemy import or_, select, update

from app.browser.whatsapp_page import WhatsAppWebPage
from app.config import settings
# bot_state_service e' importato a livello di modulo (e non dentro le funzioni
# come gli altri servizi qui sotto) perche' il kill-switch va ricontrollato
# anche dentro l'attesa della quarantena, dove non c'e' una sessione DB da cui
# partire -- e perche' un import di modulo e' l'unico punto che un test puo'
# sostituire per far finta che il canale sia stato fermato a meta' attesa.
from app.services import bot_state_service, wa_profile_lock, wa_sender, wa_timing
from app.services.wa_session import WHATSAPP_WEB_URL, _open_wa_browser
from app.services.work_enqueue import arq_redis_settings

# Quanti guasti NOSTRI consecutivi (selettori, pagina in stato inatteso) su
# chat diverse fermano il numero. Tre: sotto si rischia di fermarsi per un
# blip di rete, sopra si insiste su un DOM rotto sprecando la lista.
# Contratto §3.2.
MAX_GUASTI_CONSECUTIVI = 3

# Rete di sicurezza del drift SDD/contratto (sessione 08/08, vedi wa_sender.
# _SEGNALI_CHAT_INESISTENTE): 'nessun-messaggio-nel-pannello' non arma piu'
# guasti_consecutivi (produce skipped/no_existing_chat, guardia V2, non e'
# colpa nostra), ma un contatore GEMELLO e SEPARATO conta i
# 'no_existing_chat' consecutivi su chat diverse e ferma comunque il numero
# a 5. Piu' alto di MAX_GUASTI_CONSECUTIVI (3) perche' qui il segnale singolo
# e' ambiguo per costruzione (puo' essere un pannello lento su una
# cronologia vecchia, evento reale) -- solo una sequenza lunga distingue
# "il DOM e' cambiato" da rumore normale. SDD §guardia V2 / contratto-M2-M3
# §7 vanno aggiornati in coppia con questa costante.
MAX_NO_EXISTING_CHAT_CONSECUTIVI = 5

# Cooldown Redis applicato quando FM2 ferma il numero (Fix 3, review finale
# I4): diverso dai cooldown BREVI usati altrove per blocchi soft di
# routine, perche' qui serve un UMANO che legga l'alert Telegram (gia'
# inviato sotto) e controlli il DOM/selettore -- non un timer che si
# auto-risolve. 4 ore e' una scelta arbitraria ma documentata: abbastanza
# lunga da non auto-riattivarsi prima che qualcuno se ne accorga, abbastanza
# corta da non lasciare il numero morto per giorni se l'alert viene perso.
FM2_COOLDOWN_MINUTES = 4 * 60

# Margine fra il cap wall-clock della mini-sessione e il TTL del lucchetto
# profilo. La mini-sessione si ferma da sola PRIMA che il lock possa scadere,
# invece di confidare solo nel TTL: cosi' anche se l'heartbeat non passasse
# (Redis irraggiungibile) non esiste una finestra in cui il profilo e' aperto
# ma il lock e' libero. 5 minuti coprono la chiusura del browser piu' il
# rilascio del lock, che sono secondi.
MARGINE_CAP_SESSIONE_MIN = 5

# Fetta di attesa della quarantena. Non si dorme quindici minuti in un colpo:
# a ogni fetta si ricontrolla il kill-switch e si rinnova il lucchetto, cosi'
# uno stop premuto durante l'attesa ha effetto entro un minuto e il TTL non
# scade mai per una sessione che sta soltanto aspettando.
FETTA_ATTESA_QUARANTENA_S = 60.0


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
            WaSequenceStep.step_index == (
                cc.current_step if cc.current_step is not None else -1) + 1,
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

    Il fallback NON e' un offset fisso, e la differenza si e' pagata in
    produzione. Su Windows `zoneinfo` senza il pacchetto `tzdata` solleva
    ZoneInfoNotFoundError (Windows non ha il tz database di sistema), e il
    vecchio fallback a UTC+1 (CET) sbagliava di un'ora piena per i ~7 mesi di
    ora legale: il 12/08 alle 19:59 reali il worker credeva fossero le 18, e
    la finestra 09:00-20:00 valeva di fatto 10:00-21:00 -- il canale non
    partiva prima delle 10 e scriveva a clienti veri fino alle 21. La finestra
    oraria e' esattamente la protezione che non deve slittare.

    `datetime.now()` e' l'orologio locale di sistema: il DST lo rispetta gia',
    ed e' lo stesso orario che l'operatore legge guardando la dashboard. Per
    un MVP a fuso italiano fisso (SDD Q6) e' piu' affidabile di un offset
    scritto a mano. `tzdata` sta nei requirements e resta la via principale;
    questo e' solo il paracadute. Stesso difetto in
    manual_login._rome_utc_offset_seconds, corretto insieme a questo."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Rome")).hour
    except Exception:
        return datetime.now().hour


# Timeout con cui ARQ uccide un job (task_queue.WorkerSettings.job_timeout,
# 3600 s). Il cap wall-clock della mini-sessione deve stare sotto ANCHE questo,
# non solo sotto il TTL del lucchetto: se ARQ uccide la coroutine a meta', il
# contatto claimato resta lockato fino a wa_lock_timeout_min e il browser viene
# chiuso male. Con TTL 90 il cap era 85 minuti, cioe' oltre il timeout di ARQ:
# finche' le sessioni duravano 12-22 minuti non si notava, ma l'attesa della
# quarantena aggiunta in M5.1 le porta a 27-37 minuti mediani con una coda
# destra che ci arriva (review 07/08).
#
# Importato per valore e non da task_queue per non creare un ciclo di import
# (task_queue importa wa_worker): se cambia li', va cambiato qui, e il test
# test_a6 di test_wa_m51_adversarial.py fallisce se i due divergono.
ARQ_JOB_TIMEOUT_S = 3600


def _limite_sessione_s() -> float:
    """Durata massima wall-clock di una mini-sessione, sotto il TTL del
    lucchetto profilo E sotto il timeout con cui ARQ uccide il job. Il minimo
    di 60s evita che una configurazione con TTL piccolo produca un limite nullo
    o negativo (che chiuderebbe ogni sessione prima del primo messaggio)."""
    da_lucchetto = (int(settings.wa_profile_lock_ttl_min)
                    - MARGINE_CAP_SESSIONE_MIN) * 60.0
    da_arq = ARQ_JOB_TIMEOUT_S - MARGINE_CAP_SESSIONE_MIN * 60.0
    return max(60.0, min(da_lucchetto, da_arq))


async def _niente_da_fare_prima_del_browser(number_id: str) -> str | None:
    """Ritorna il motivo per cui NON vale la pena aprire il browser, o None.

    Ricalca i cancelli che la mini-sessione applica comunque a ogni messaggio
    (finestra oraria, cap del numero, esistenza di lavoro eleggibile), ma li
    guarda PRIMA, quando costano due query invece di quindici minuti di
    profilo occupato. Non li sostituisce: dentro il loop restano, con query
    live, perche' una sessione dura decine di minuti e nel frattempo tutto puo'
    cambiare (contratto §7.2). Questo e' solo un filtro d'ingresso.

    Deliberatamente NON prende il lucchetto e NON claima niente: un pre-check
    che lockasse una riga la terrebbe ferma per tutta la quarantena.
    """
    from datetime import datetime as _dt

    from sqlalchemy import func

    from app.database import AsyncSessionLocal
    from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                               WaContact, WaContactStatus, WaNumber)
    from app.services import wa_number_manager

    async with AsyncSessionLocal() as db:
        campagna = await db.scalar(
            select(WaCampaign).where(WaCampaign.wa_number_id == number_id,
                                     WaCampaign.status == WaCampaignStatus.running)
        )
        if campagna is None:
            return "niente_da_fare"

        ora = _ora_locale_corrente()
        inizio, fine = wa_timing.effective_wa_active_hours(campagna)
        if not (inizio <= ora < fine):
            return "fuori_finestra"

        number = await db.scalar(select(WaNumber).where(WaNumber.id == number_id))
        if number is None:
            return "numero_non_attivo"
        if not await wa_number_manager.has_wa_send_budget(db, number, campagna):
            return "cap_esaurito"

        now = _dt.utcnow()
        stale_cutoff = now - timedelta(minutes=int(settings.wa_lock_timeout_min))
        pronti = await db.scalar(
            select(func.count(WaCampaignContact.id))
            .join(WaContact, WaContact.id == WaCampaignContact.contact_id)
            .where(
                WaCampaignContact.campaign_id == campagna.id,
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
        ) or 0
        if not pronti:
            return "niente_da_fare"

    return None


async def _attendi_quarantena_risync(number_id: str, lock_token: str,
                                     browser_t0: float) -> str | None:
    """Aspetta che scada la quarantena post-riconnessione prima del primo
    invio della sessione. Ritorna None se e' andata a buon fine, altrimenti il
    motivo per cui la mini-sessione deve chiudersi subito.

    Perche' un'ATTESA e non un rifiuto (review 07/08, blocco B1). La guardia
    di wa_sender rifiuta l'invio finche' il browser e' vivo da meno di
    WA_RESYNC_QUARANTINE_MIN minuti. Ma ogni mini-sessione apre un browser
    NUOVO: il contatore riparte da zero ogni volta e la quarantena non scade
    mai. E ogni rifiuto tornava 'queued', che il worker contava come guasto
    nostro: dopo tre (2-4 minuti, col delay mediano di 90 s) scattava FM2.
    Misurato sul codice e sulla config veri: succedeva nel 99,8% delle
    sessioni -- cioe' il canale non poteva inviare niente a nessuno.

    Il contratto 3.4.2 dice "nei primi N minuti il numero non invia", ed e'
    esattamente questo: si sta fermi, non si consumano contatti per farseli
    rifiutare. La guardia in wa_sender resta dov'e' come seconda rete: se un
    domani questo flusso cambia, la protezione regge comunque.

    Costo: ~15 minuti di browser aperto e fermo per mini-sessione. Sta sotto
    il cap wall-clock della sessione (85 min) e sotto il TTL del lucchetto
    (90 min), che qui si rinnova a ogni fetta.
    """
    quarantena_s = float(settings.wa_resync_quarantine_min) * 60
    if quarantena_s <= 0:
        return None

    # Una quarantena piu' lunga del cap wall-clock della sessione terrebbe il
    # profilo aperto oltre il TTL del lucchetto senza mandare un solo
    # messaggio: aspetteremmo con un browser vivo un tempo che, per
    # costruzione, non lascia spazio all'invio. Non e' uno scenario ipotetico
    # -- basta scrivere 120 in WA_RESYNC_QUARANTINE_MIN con il TTL a 90. Si
    # esce subito e in modo rumoroso, invece di occupare il profilo per ore.
    if quarantena_s >= _limite_sessione_s():
        logger.error(
            f"[WA] {number_id}: WA_RESYNC_QUARANTINE_MIN="
            f"{settings.wa_resync_quarantine_min} min e' >= del cap di sessione "
            f"({round(_limite_sessione_s() / 60)} min, derivato da "
            f"WA_PROFILE_LOCK_TTL_MIN): cosi' configurato il numero non potrebbe "
            "inviare nulla. Sessione annullata, correggi la configurazione.")
        return "quarantena_oltre_cap_sessione"

    residuo = quarantena_s - (time.perf_counter() - browser_t0)
    if residuo <= 0:
        return None

    logger.info(f"[WA] {number_id}: quarantena di risincronizzazione, attendo "
                f"{round(residuo / 60)} min prima del primo invio. Non e' un "
                "blocco: la sessione WhatsApp Web si sta risincronizzando e "
                "finche' non ha finito la guardia opt-out leggerebbe il vuoto "
                "invece del silenzio.")

    while residuo > 0:
        if await bot_state_service.is_wa_halted():
            logger.info(f"[WA] {number_id}: quarantena interrotta dal kill-switch")
            return "wa_halted"
        await asyncio.sleep(min(FETTA_ATTESA_QUARANTENA_S, residuo))
        if not await wa_profile_lock.renew(number_id, lock_token):
            # Il lucchetto non e' piu' nostro: qualcun altro ha legittimamente
            # acquisito il profilo (TTL scaduto per un blip Redis, o un altro
            # consumatore). Proseguire vorrebbe dire un secondo Chromium sullo
            # stesso user-data-dir, cioe' la corruzione del profilo che il
            # lucchetto esiste per impedire -- e qui il rischio e' massimo,
            # perche' l'attesa dura un quarto d'ora senza nessuno che guardi.
            logger.warning(f"[WA] {number_id}: lucchetto profilo perso durante "
                           "la quarantena, sessione annullata")
            return "profilo_occupato"
        residuo = quarantena_s - (time.perf_counter() - browser_t0)

    return None


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
    from app.services import wa_number_manager

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

    # Cancello 2: c'e' davvero qualcosa da fare? Costa due query e si fa PRIMA
    # di aprire il browser, perche' da M5.1 aprire il browser significa anche
    # restare fermi quindici minuti ad aspettare la quarantena. Senza questo
    # controllo, un numero col cap giornaliero esaurito (o fuori finestra
    # oraria) apriva WhatsApp Web, teneva il lucchetto del profilo per un
    # quarto d'ora senza mandare niente, chiudeva, e ricominciava dopo il break
    # -- tutta la notte, perche' nessuno gatta wa_send_task per ora. Costo
    # doppio: l'health-check e il reply-scan trovavano il profilo occupato e
    # saltavano il giro, e una sessione WhatsApp Web autenticata aperta a vuoto
    # venti volte per notte e' esattamente il footprint che il pacing esiste
    # per evitare (review 07/08 su M5.1).
    motivo_precheck = await _niente_da_fare_prima_del_browser(number_id)
    if motivo_precheck is not None:
        esito["motivo"] = motivo_precheck
        return esito

    quanti = None          # calcolato dopo il primo claim, sulla campagna vera
    processati = 0
    guasti_consecutivi = 0
    no_existing_chat_consecutivi = 0

    try:
        async with wa_profile_lock.held(number_id) as lock_token:
            async with _open_wa_browser(number_id, headless=settings.wa_send_headless,
                                        proxy_url=proxy_url) as context:
                page = await context.new_page()
                await page.goto(WHATSAPP_WEB_URL, wait_until="domcontentloaded")
                pom = WhatsAppWebPage(page)
                browser_t0 = time.perf_counter()
                limite_s = _limite_sessione_s()

                # La quarantena si aspetta QUI, una volta, invece di essere
                # scoperta contatto per contatto dalla guardia -- che li
                # rifiuterebbe tutti e armerebbe FM2 dopo tre (blocco B1 della
                # review 07/08). La guardia resta comunque attiva a valle.
                motivo_quarantena = await _attendi_quarantena_risync(
                    number_id, lock_token, browser_t0)
                if motivo_quarantena is not None:
                    esito["motivo"] = motivo_quarantena
                    return esito

                while quanti is None or processati < quanti:
                    # Cap wall-clock: la durata di una sessione ha una coda destra
                    # lunga (delay lognormale + costo di invio), e sforare il TTL
                    # del lucchetto significa un secondo Chromium sullo stesso
                    # profilo. Si esce pulito prima, il worker rischedula dopo il
                    # break come per qualunque altra sessione conclusa.
                    if time.perf_counter() - browser_t0 >= limite_s:
                        esito["motivo"] = "timeout_sessione"
                        break

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
                        # Catturati SUBITO: un rollback piu' sotto (Fix B, review
                        # finale round 2) espira tutti gli oggetti ORM della
                        # sessione -- rileggere cc.id/campaign.id DOPO quel rollback
                        # dichiara un lazy-load implicito che l'AsyncSession non
                        # puo' eseguire fuori da un await esplicito (MissingGreenlet).
                        # Gli id, letti ora mentre gli oggetti sono ancora "freschi",
                        # restano validi indipendentemente da cosa succede alla
                        # sessione dopo.
                        cc_id, contact_id, campaign_id = cc.id, contact.id, campaign.id

                        if quanti is None:
                            quanti = wa_timing.wa_session_message_count(campaign)

                        ora = _ora_locale_corrente()
                        inizio, fine = wa_timing.effective_wa_active_hours(campaign)
                        if not (inizio <= ora < fine):
                            await _rilascia_lock(db, cc_id)
                            esito["motivo"] = "fuori_finestra"
                            break

                        if not await wa_number_manager.has_wa_send_budget(db, number, campaign):
                            await _rilascia_lock(db, cc_id)
                            esito["motivo"] = "cap_esaurito"
                            break

                        try:
                            res = await wa_sender.invia_a_contatto(
                                db, pom, campaign=campaign, step=step, cc=cc, contact=contact,
                                number=number,
                                browser_avviato_da_s=time.perf_counter() - browser_t0)
                        except Exception as exc:
                            # Un'eccezione IMPREVISTA (decrypt, commit DB, blip) non
                            # deve mai propagare fuori dalla mini-sessione: senza
                            # questo except fermava TUTTO il job in silenzio, il
                            # contatto restava lockato fino al prossimo health-check
                            # (20 min) e _rischedula non veniva mai raggiunta -- il
                            # numero smetteva di inviare senza un log che lo
                            # spiegasse (Fix 4, review finale I7). Si tratta come
                            # 'queued': stesso ramo del guasto nostro qui sotto,
                            # rilascia il lock e arma FM2. Nessun numero in chiaro:
                            # solo l'id opaco del contatto.
                            #
                            # Rollback PRIMA di tutto (review finale round 2, gap
                            # residuo su Fix 4): se l'eccezione viene da un
                            # db.commit() fallito dentro invia_a_contatto, la
                            # AsyncSession resta in stato must-rollback -- la
                            # prossima istruzione che tocca db (_rilascia_lock, qui
                            # sotto) solleverebbe PendingRollbackError FUORI da
                            # questo except, ripresentando I7 identico per la classe
                            # di eccezione piu' citata nel finding originale.
                            # Difensivo (stesso pattern di browser_bio._resilient_
                            # release e bot_state_service.halt/resume): un rollback
                            # su una sessione gia' pulita e' un no-op innocuo.
                            try:
                                await db.rollback()
                            except Exception:
                                pass
                            logger.error(f"[WA] invia_a_contatto: eccezione imprevista su "
                                        f"contatto {contact_id} (cc={cc_id}): "
                                        f"{type(exc).__name__}")
                            res = wa_sender.EsitoInvio("queued", f"eccezione:{type(exc).__name__}")
                        processati += 1

                        if res.stato == "sent":
                            esito["inviati"] += 1
                            guasti_consecutivi = 0
                            # Un invio riuscito e' l'UNICO evento che azzera
                            # questo contatore (decisione esplicita, drift
                            # SDD): gli altri 'skipped'/'failed' di mezzo non
                            # lo toccano, di proposito -- e' dedicato a UNA
                            # sequenza dello stesso segnale, non un secondo
                            # guasti_consecutivi generico.
                            no_existing_chat_consecutivi = 0
                        elif res.stato in ("skipped", "opted_out", "replied"):
                            esito["saltati"] += 1
                            guasti_consecutivi = 0
                            if res.stato == "skipped" and res.motivo == "no_existing_chat":
                                no_existing_chat_consecutivi += 1
                        elif res.stato == "failed":
                            esito["falliti"] += 1
                            guasti_consecutivi = 0
                        else:  # 'queued' = il contatto non si tocca
                            await _rilascia_lock(db, cc_id)
                            # Non tutti i 'queued' sono guasti del DOM, e chi
                            # costruisce l'esito lo sa: lo dichiara in
                            # `arma_fm2` (default True, fail-closed). Il worker
                            # non ricostruisce quel giudizio da una lista di
                            # motivi -- ci ha provato in M5.1 e si e' perso
                            # 'ricerca_senza_risultati', che e' un fatto sul
                            # contatto e non sul nostro DOM: tre numeri non su
                            # WhatsApp di fila fermavano il numero per quattro
                            # ore dicendo "probabile DOM cambiato".
                            if res.arma_fm2:
                                guasti_consecutivi += 1

                        if guasti_consecutivi >= MAX_GUASTI_CONSECUTIVI:
                            await _ferma_numero_per_guasto(db, number_id, campaign_id,
                                                           guasti_consecutivi)
                            esito["motivo"] = "guasti_consecutivi"
                            break
                        if no_existing_chat_consecutivi >= MAX_NO_EXISTING_CHAT_CONSECUTIVI:
                            await _ferma_numero_per_guasto(
                                db, number_id, campaign_id, no_existing_chat_consecutivi,
                                causa="segnali 'no_existing_chat' consecutivi "
                                      "(probabile DOM cambiato proprio su quel selettore, "
                                      "non i contatti)")
                            esito["motivo"] = "no_existing_chat_consecutivi"
                            break

                    # Heartbeat del lucchetto: rimette il TTL pieno ora che c'e'
                    # un segno di vita fresco, cosi' la scadenza dipende
                    # dall'ultimo messaggio e non dalla durata totale prevista
                    # della sessione (che ha una coda destra lunga). Un EXPIRE.
                    await wa_profile_lock.renew(number_id, lock_token)

                    # Delay lognormale FRA i messaggi, dentro la sessione. Non e' un
                    # "sleep lungo": e' la mediana di 90s che rende il ritmo umano.
                    await asyncio.sleep(wa_timing.wa_send_delay_seconds())
    except wa_profile_lock.WaProfileBusy:
        esito["motivo"] = "profilo_occupato"
        return esito

    logger.info(f"[WA] mini-sessione {number_id}: {esito}")
    return esito


async def _rilascia_lock(db, cc_id: str) -> None:
    """Prende l'id, non l'oggetto ORM: dopo un rollback (Fix B, review
    finale round 2) l'oggetto e' expired e rileggerne un attributo qui
    dentro solleverebbe un lazy-load implicito (MissingGreenlet in
    AsyncSession). L'id e' un valore semplice, indipendente dallo stato
    della sessione."""
    from app.models.wa import WaCampaignContact
    await db.execute(update(WaCampaignContact).where(WaCampaignContact.id == cc_id)
                     .values(locked_by=None, locked_at=None))
    await db.commit()


async def _ferma_numero_per_guasto(db, number_id: str, campaign_id: str, n: int, *,
                                   causa: str = "guasti consecutivi (probabile DOM cambiato)") -> None:
    """FM2: N eventi consecutivi su chat diverse che indicano un problema
    NOSTRO (DOM cambiato o pagina in stato inatteso) fermano il numero e
    mettono la campagna in error; i contatti restano intatti (queued o,
    nel caso no_existing_chat, gia' correttamente skipped) perche' NON e'
    colpa loro. Un selettore rotto non deve bruciare una lista (SDD 11).

    Due contatori distinti chiamano questa funzione (esegui_mini_sessione):
    guasti_consecutivi (guasti DOM classici, soglia 3) e
    no_existing_chat_consecutivi (drift SDD, soglia 5, vedi wa_sender.py).
    `causa` distingue il log/alert fra i due invece di dire sempre lo stesso
    "probabile DOM cambiato" generico."""
    from app.models.wa import WaCampaign, WaCampaignStatus, WaNumber, WaNumberStatus
    from app.services import notifier, wa_number_manager
    from app.utils import events

    await db.execute(update(WaNumber).where(WaNumber.id == number_id)
                     .values(status=WaNumberStatus.cooldown))
    await db.execute(update(WaCampaign).where(WaCampaign.id == campaign_id)
                     .values(status=WaCampaignStatus.error))
    await db.commit()
    # Senza questa chiamata la chiave Redis con TTL non esiste: il prossimo
    # health-check (release_expired_wa_cooldowns) trova il numero in
    # status='cooldown', non trova la chiave, conclude che il cooldown e'
    # "scaduto" e lo rimette 'active' da solo entro 30 min -- annullando lo
    # stop (Fix 3, review finale I4).
    await wa_number_manager.apply_wa_cooldown(number_id, minutes=FM2_COOLDOWN_MINUTES)
    events.emit(campaign_id, "wa.number.stopped",
                f"{n} {causa}: numero fermato, contatti intatti",
                level="error")
    await notifier.send_telegram(
        f"WhatsApp: numero fermato dopo {n} {causa}. "
        f"Campagna in error, contatti NON bruciati.",
        level="error")


def wa_send_job_id(number_id: str) -> str:
    """Un solo job di invio per numero (SDD Q2: max 1 campagna running per
    numero). ARQ scarta il duplicato da solo -- FM11."""
    return f"wa:send:{number_id}"


async def _campagna_attiva_del_numero(number_id: str):
    """La campagna `running` sul numero, se c'e' -- usata solo per leggere
    gli override per-campagna del break (session_break_seconds legge i campi
    break_min/max_minutes da qui, con getattr: None e' un fallback valido se
    la campagna e' finita nel frattempo)."""
    from app.database import AsyncSessionLocal
    from app.models.wa import WaCampaign, WaCampaignStatus

    async with AsyncSessionLocal() as db:
        return await db.scalar(
            select(WaCampaign).where(WaCampaign.wa_number_id == number_id,
                                     WaCampaign.status == WaCampaignStatus.running)
        )


async def _chiudi_campagna_se_finita(number_id: str) -> str | None:
    """Porta a 'completed' la campagna running del numero, se non le resta
    nessuna riga non terminale. Ritorna l'id chiuso, o None.

    Prima di M5.1 nessuno scriveva mai questo stato: il contratto 4.1 lo
    assegna a M3 e M3 non lo aveva implementato (verificato con grep su tutto
    app/, review 07/08). Una campagna finita restava "In corso" nella UI per
    sempre, e il cliente non aveva modo di sapere che era conclusa.

    Una riga con next_action_at nel FUTURO non e' lavoro finito, e' lavoro
    rimandato: la campagna resta running e sara' il supervisore
    (cron_worker.wa_campaign_supervisor) a riaccodarla quando l'appuntamento
    arriva. In MVP lo step e' uno solo e il caso non si presenta, ma scriverlo
    cosi' costa niente e non lascia una trappola al multi-step.
    """
    from sqlalchemy import func

    from app.database import AsyncSessionLocal
    from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                               WaContactStatus)
    from app.services import notifier
    from app.utils import events

    async with AsyncSessionLocal() as db:
        campagna = await db.scalar(
            select(WaCampaign).where(WaCampaign.wa_number_id == number_id,
                                     WaCampaign.status == WaCampaignStatus.running)
        )
        if campagna is None:
            return None

        rimaste = await db.scalar(
            select(func.count(WaCampaignContact.id)).where(
                WaCampaignContact.campaign_id == campagna.id,
                WaCampaignContact.status.in_([WaContactStatus.queued,
                                              WaContactStatus.in_sequence]),
            )
        ) or 0
        if rimaste:
            return None

        campaign_id, nome, inviati = campagna.id, campagna.name, campagna.sent
        # UPDATE condizionale, non `campagna.status = ...`: due mini-sessioni
        # dello stesso numero che finiscono insieme farebbero entrambe la
        # SELECT sopra vedendo 'running', ed entrambe scriverebbero -- due
        # eventi, due Telegram, due completed_at diversi per la stessa
        # campagna. Ripetendo la condizione dentro la scrittura, la seconda
        # trova rowcount 0 e si ferma. Stesso schema dell'UPDATE atomico che
        # protegge "una sola campagna running per numero" in
        # wa_campaign_service.avvia.
        #
        # Copre anche il caso piu' probabile in pratica: una campagna messa in
        # pausa da un umano fra la SELECT e la scrittura non deve diventare
        # 'completed'.
        chiusura = await db.execute(
            update(WaCampaign)
            .where(WaCampaign.id == campaign_id,
                   WaCampaign.status == WaCampaignStatus.running)
            .values(status=WaCampaignStatus.completed,
                    completed_at=datetime.utcnow())
        )
        await db.commit()
        if (chiusura.rowcount or 0) == 0:
            return None

    events.emit(campaign_id, "wa.campaign.completed",
                f"campagna conclusa: {inviati} messaggi inviati")
    logger.info(f"[WA] campagna {campaign_id} conclusa ({inviati} inviati)")
    await notifier.send_telegram(
        f'WhatsApp: campagna "{nome}" conclusa. {inviati} messaggi inviati.')
    return campaign_id


async def wa_send_task(ctx: dict, number_id: str) -> None:
    """Task ARQ. Esce SEMPRE presto: la pausa fra mini-sessioni non si fa
    dormendo dentro il job (lezione job_timeout della Fase Bio), si fa
    rischedulando.

    La rischedulazione e' un `Retry(defer=...)` sollevato da QUESTA
    invocazione, non un `enqueue_job` con lo stesso `_job_id` chiamato da
    dentro il job ancora in esecuzione (bug review finale, round 2): la
    chiave Redis `arq:job:{job_id}` resta viva fino a `finish_job`, che gira
    DOPO il return della coroutine, quindi un `enqueue_job` con lo stesso id
    fatto da qui dentro trova la chiave gia' presente e ARQ lo scarta in
    silenzio come duplicato (`None`, nessuna eccezione) -- il numero manda
    una mini-sessione e poi non invia mai piu' finche' un umano non richiama
    enqueue_wa_workers a mano. `Retry` sidesteppa il problema: non chiama
    enqueue_job, fa rischedulare il job CORRENTE al worker ARQ dopo che la
    coroutine e' uscita (stesso calco di browser_bio.py dichiarato nel
    docstring del modulo, vedi browser_bio_account_task in task_queue.py).
    Richiede max_tries alto in WorkerSettings: ogni break e' un "try" agli
    occhi di ARQ."""
    from arq.jobs import Job          # noqa: F401  (documenta la dipendenza)
    from arq.worker import Retry
    from app.services import wa_timing

    esito = await esegui_mini_sessione(number_id)

    # 'quarantena_oltre_cap_sessione' e' terminale come gli altri: e' una
    # configurazione incoerente, e rischedularla ogni venti minuti produrrebbe
    # solo un browser aperto a vuoto piu' volte all'ora finche' un umano non
    # legge il log. Meglio fermarsi e farsi notare.
    if esito["motivo"] in ("send_disabled", "wa_halted", "numero_non_attivo",
                           "guasti_consecutivi", "no_existing_chat_consecutivi",
                           "niente_da_fare", "quarantena_oltre_cap_sessione"):
        if esito["motivo"] == "niente_da_fare":
            # Unico momento in cui si sa che non e' rimasto lavoro da fare: e'
            # qui che una campagna finita smette di sembrare "In corso" per
            # sempre (M5.1). Gli altri motivi terminali dicono "non posso
            # lavorare adesso", non "non c'e' piu' lavoro".
            await _chiudi_campagna_se_finita(number_id)
        logger.info(f"[WA] {number_id}: sessione chiusa ({esito['motivo']}), "
                    "nessuna rischedulazione automatica")
        return

    if esito["motivo"] == "profilo_occupato":
        logger.info(f"[WA] {number_id}: profilo occupato (health-check o "
                    "reply-scan in corso), riprovo fra "
                    f"{settings.wa_lock_busy_retry_s}s")
        raise Retry(defer=int(settings.wa_lock_busy_retry_s))

    # cap_esaurito / fuori_finestra / timeout_sessione / completata -> si
    # riprende dopo il break.
    break_s = wa_timing.wa_session_break_seconds(
        await _campagna_attiva_del_numero(number_id))
    raise Retry(defer=int(break_s))


async def enqueue_wa_workers(campaign_id: str, *,
                             anticipa_se_differito: bool = False) -> int:
    """Fan-out: un job per numero della campagna (in MVP il numero e' uno).
    Stessa forma di work_enqueue.enqueue_dm_workers_with_redis.

    `anticipa_se_differito` distingue CHI sta accodando, e non e' un dettaglio:
    il 12/08 una campagna ripresa a mano e' rimasta ferma senza che niente
    segnalasse un problema. Il job ha un _job_id fisso per numero e ARQ scarta
    in silenzio i duplicati, quindi il job differito dal break anti-ban restava
    in coda con lo score vecchio (+16 minuti) mentre la UI diceva "In corso".

    - Flusso AUTOMATICO (default False): non si tocca niente. Li' un job
      differito lo e' per una ragione -- cap esaurito, fuori finestra, break di
      sessione -- e anticiparlo a ogni giro del supervisore annullerebbe il
      pacing.
    - Azione UMANA (avvia/riprendi/kick): si anticipa lo score. Chi preme
      "riprendi" deve vedere la campagna ripartire.

    Mai a tempo zero, pero': si anticipa a `adesso + wa_send_delay_seconds()`.
    Il break e' una protezione anti-ban, non un ritardo da saltare -- ripartire
    attaccati al messaggio precedente e' la firma che WhatsApp cerca. Si taglia
    l'attesa lunga, non il delay fra due invii."""
    from app.database import AsyncSessionLocal
    from app.models.wa import WaCampaign

    async with AsyncSessionLocal() as db:
        campaign = await db.scalar(select(WaCampaign).where(WaCampaign.id == campaign_id))
        if campaign is None:
            return 0
        number_ids = [campaign.wa_number_id]

    redis = await arq.create_pool(arq_redis_settings())
    try:
        n = 0
        for number_id in number_ids:
            job = await redis.enqueue_job("wa_send_task", number_id,
                                          _job_id=wa_send_job_id(number_id))
            if job is not None:
                n += 1
            elif anticipa_se_differito:
                # Il job c'e' gia': lo si sposta, non se ne crea un altro --
                # l'id resta uno solo e l'invariante "un solo job per numero"
                # regge. Si anticipa solo se lo score e' PIU' LONTANO del
                # delay minimo: un job gia' imminente non va toccato, sarebbe
                # solo un modo di anticiparlo sotto la soglia anti-ban.
                import time as _time

                from app.services import wa_timing

                job_id = wa_send_job_id(number_id)
                quando = _time.time() * 1000 + wa_timing.wa_send_delay_seconds() * 1000
                score = await redis.zscore("arq:queue", job_id)
                if score is not None and score > quando:
                    await redis.zadd("arq:queue", {job_id: quando})
                    n += 1
                    logger.info(f"[WA] enqueue {number_id}: job gia' schedulato fra "
                                f"{(score - _time.time() * 1000) / 1000:.0f}s, "
                                "anticipato su richiesta esplicita")
                else:
                    logger.info(f"[WA] enqueue {number_id}: job gia' imminente, "
                                "lasciato dov'e'")
            else:
                # ARQ scarta in silenzio un enqueue con _job_id gia' presente
                # (job differito da un Retry precedente, es. dopo
                # cap_esaurito/fuori_finestra) -- corretto per il flusso
                # automatico (evita duplicati), ma un umano che chiama
                # avvia()/riprendi()/kick e vede "accodati:0" non ha modo di
                # distinguerlo da un guasto vero senza questo log (lezione
                # collaudo A3, 07/08). Per forzarlo subito senza duplicarlo:
                # ZADD arq:queue <now_ms> wa:send:<number_id>.
                logger.info(f"[WA] enqueue {number_id}: job "
                            f"{wa_send_job_id(number_id)} gia' schedulato, "
                            "non e' un errore -- lo raccoglie da solo al suo turno")
        return n
    finally:
        await redis.aclose()


async def recover_wa_sending_on_startup() -> int:
    """FM14: al riavvio, i wa_messages rimasti 'sending' sono lavoro appeso
    di cui non si sa se e' partito davvero (il processo puo' essere caduto
    fra send_text() riuscito e il commit che lo registra 'sent'). Decisione
    esplicita di Tommaso (round1 post-review): MAI riprovare in dubbio --
    meglio un messaggio che non parte che uno che parte due volte.

    Il messaggio si marca failed (il dato resta onesto). SOLO il
    wa_campaign_contacts legato a QUEL messaggio (via campaign_id +
    contact_id) va fermato in modo terminale (skipped, non rieleggibile):
    NON si tocca do_not_contact, perche' il contatto potrebbe essere
    perfettamente raggiungibile -- e' solo questo UN tentativo di consegna
    ad essere ambiguo, una campagna futura puo' riprovarci.

    Non si rilasciano gli altri lock stantii qui: quello e' compito del
    health-check periodico (cron_worker.wa_health_check, timeout
    wa_lock_timeout_min), che copre un caso diverso (worker morto senza un
    invio in corso) e ha gia' la sua logica -- non va duplicata ne'
    ristretta da questa funzione.

    G5 (fix, sessione 08/08): l'UPDATE sul contatto filtra ORA anche per il
    suo stato ATTUALE (queued/in_sequence, cioe' "ancora in lavorazione").
    Prima non filtrava affatto: un contatto gia' servito con successo
    (completed/replied) da un ALTRO step/messaggio, ma con un messaggio
    orfano 'sending' rimasto in giro, veniva declassato a 'skipped' --
    corrompendo i conteggi della campagna e la sua chiusura automatica.
    Interazione con la guardia anti-doppio-invio (PR #52, wa_sender.py
    invia_a_contatto): quella guardia, trovando gia' un WaMessage
    'sending'/'sent' per la stessa tripla (campagna, contatto, step), marca
    il contatto 'skipped' ma LASCIA il messaggio in 'sending' -- al riavvio
    successivo questa funzione lo ritrova. Col filtro di stato, 'skipped'
    non e' piu' fra gli stati toccati: il secondo passaggio diventa un
    no-op sul contatto invece di ri-scrivere sopra la decisione gia' presa
    dalla guardia."""
    from app.database import AsyncSessionLocal
    from app.models.wa import WaCampaignContact, WaContactStatus, WaMessage, WaMessageStatus

    async with AsyncSessionLocal() as db:
        appesi = (await db.execute(
            select(WaMessage).where(WaMessage.status == WaMessageStatus.sending)
        )).scalars().all()
        for msg in appesi:
            msg.status = WaMessageStatus.failed
            msg.error = "recovery: processo interrotto durante l'invio (stato reale ignoto)"
            await db.execute(
                update(WaCampaignContact)
                .where(WaCampaignContact.campaign_id == msg.campaign_id,
                       WaCampaignContact.contact_id == msg.contact_id,
                       WaCampaignContact.status.in_((WaContactStatus.queued,
                                                     WaContactStatus.in_sequence)))
                .values(status=WaContactStatus.skipped,
                       next_action_at=None,
                       locked_by=None,
                       locked_at=None,
                       last_error="recovery: stato di invio ambiguo dopo un riavvio, "
                                  "contatto fermato per sicurezza -- verificare "
                                  "manualmente se il messaggio e' arrivato")
            )
        await db.commit()
        if appesi:
            logger.warning(f"[WA] recovery avvio: {len(appesi)} messaggi 'sending' "
                           "chiusi come failed, altrettanti contatti fermati "
                           "(skipped, non riprovati -- verifica manuale consigliata)")
        return len(appesi)
