"""Dedicated ARQ worker for cron jobs.

Run: arq app.workers.cron_worker.CronWorkerSettings
"""
import httpx
from arq import cron

from app.config import settings
from app.services.work_enqueue import ARQ_CRON_QUEUE, arq_redis_settings
from app.services import wa_profile_lock, wa_reply_watcher
from app.services.wa_session import check_session
from app.workers.task_queue import (
    check_replies,
    daily_reset,
    recover_sending,
    release_stale_locks,
    telegram_commands,
)


async def wa_session_healthcheck(ctx: dict) -> dict:
    """Ogni 30 minuti nelle ore attive (SDD Q56): per ogni numero non
    ritirato, guarda se la sessione e' viva; se e' caduta mette in pausa le
    sue campagne e avvisa. In piu' rilascia cooldown scaduti e lock stale.

    Il check apre il browser headless (check_session, M1): e' l'operazione
    piu' cara di questo cron, ed e' il motivo per cui gira su un cron
    dedicato e non dentro il worker di invio. Prima di aprirlo acquisisce
    il lucchetto Redis del profilo (wa_profile_lock, M4): se invio o
    reply-scan lo stanno gia' usando, salta il numero invece di rischiare
    un secondo Chromium concorrente sullo stesso profilo.
    """
    from app.database import AsyncSessionLocal
    from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                               WaNumber, WaNumberStatus)
    from app.services import notifier, wa_number_manager
    from app.config import settings
    from datetime import datetime, timedelta
    from loguru import logger
    from sqlalchemy import select, update

    esito = {"controllati": 0, "caduti": 0, "cooldown_rilasciati": 0,
             "lock_rilasciati": 0, "saltati_invio_attivo": 0}

    async with AsyncSessionLocal() as db:
        numeri = (await db.execute(
            select(WaNumber).where(WaNumber.status.notin_([
                WaNumberStatus.retired, WaNumberStatus.suspended,
                WaNumberStatus.pending_qr]))
        )).scalars().all()
        ids = [n.id for n in numeri]

    for number_id in ids:
        try:
            async with wa_profile_lock.held(number_id):
                esito["controllati"] += 1
                try:
                    stato = await check_session(number_id)
                except Exception as exc:
                    logger.error(f"[WA] health-check {number_id} fallito: {type(exc).__name__}")
                    continue
                if stato == WaNumberStatus.active:
                    continue
                esito["caduti"] += 1
                async with AsyncSessionLocal() as db:
                    await db.execute(
                        update(WaCampaign)
                        .where(WaCampaign.wa_number_id == number_id,
                               WaCampaign.status == WaCampaignStatus.running)
                        .values(status=WaCampaignStatus.paused)
                    )
                    await db.commit()
                await notifier.send_telegram(
                    f"WhatsApp: numero {number_id[:8]} -> {stato.value}. "
                    "Campagne in pausa. Serve un nuovo QR (lo scansiona il cliente).",
                    level="error")
        except wa_profile_lock.WaProfileBusy:
            esito["saltati_invio_attivo"] += 1
            logger.info(f"[WA] health-check {number_id[:8]} saltato: "
                       "profilo occupato (invio o reply-scan in corso)")
        except Exception as exc:
            # Qualunque altro guasto sul singolo numero (es. un blip Redis in
            # held()) non deve abortire il run intero: sotto questo loop girano
            # il rilascio dei cooldown scaduti e dei lock stale, che valgono
            # anche quando un numero e' irraggiungibile. Stesso pattern
            # per-numero gia' in uso in wa_reply_scan.
            logger.error(f"[WA] health-check {number_id[:8]} saltato per un "
                         f"guasto: {type(exc).__name__}")

    try:
        # Stesso motivo del try per-numero qui sopra: questa chiamata legge una
        # chiave Redis per ogni numero in cooldown, e un Redis irraggiungibile
        # abortiva il run PRIMA del rilascio dei lock stale qui sotto -- che e'
        # lavoro puramente DB e non ha motivo di dipendere da Redis.
        esito["cooldown_rilasciati"] = len(
            await wa_number_manager.release_expired_wa_cooldowns())
    except Exception as exc:
        logger.error("[WA] health-check: rilascio cooldown saltato "
                     f"({type(exc).__name__}), proseguo con i lock stale")

    async with AsyncSessionLocal() as db:
        cutoff = datetime.utcnow() - timedelta(minutes=int(settings.wa_lock_timeout_min))
        res = await db.execute(
            update(WaCampaignContact)
            .where(WaCampaignContact.locked_by.is_not(None),
                   WaCampaignContact.locked_at < cutoff)
            .values(locked_by=None, locked_at=None)
        )
        await db.commit()
        esito["lock_rilasciati"] = res.rowcount or 0

    logger.info(f"[WA] health-check: {esito}")
    return esito


async def wa_reply_scan(ctx: dict) -> dict:
    """Ogni scan: solo numeri con lavoro da fare secondo
    wa_reply_watcher.numeri_da_scansionare (campagna running con contatti
    queued/in_sequence, oppure un invio recente).

    La schedulazione resta dentro le ore attive come l'health-check, ma qui
    NON si ricontrolla la finestra oraria a runtime (a differenza di
    esegui_mini_sessione): una scansione non manda nulla, quindi non c'e'
    niente da tenere dentro l'orario umano.

    Non e' tempo-critico per l'MVP (campagne a 1 messaggio, Q29): serve per
    KPI e per la rete di opt-out, la garanzia vera resta la guardia
    pre-invio (§7.5 punto 7)."""
    from app.database import AsyncSessionLocal
    from loguru import logger

    esito = {"numeri_scansionati": 0, "optout_totali": 0, "replied_totali": 0}
    async with AsyncSessionLocal() as db:
        ids = await wa_reply_watcher.numeri_da_scansionare(db)

    for number_id in ids:
        try:
            risultato = await wa_reply_watcher.scan_number(number_id)
        except Exception as exc:
            # Il messaggio si logga SOLO per RuntimeError: e' l'eccezione con
            # cui scan_chat_list dice QUALE selettore e' disallineato, e senza
            # quella diagnosi il log non serve a riparare nulla. Per le altre
            # resta il solo tipo: potrebbero portarsi dietro una preview di
            # conversazione (PII).
            dettaglio = f" -- {exc}" if isinstance(exc, RuntimeError) else ""
            logger.error(f"[WA] reply-scan {number_id} fallito: "
                         f"{type(exc).__name__}{dettaglio}")
            continue
        if risultato["motivo"] is None:
            esito["numeri_scansionati"] += 1
        esito["optout_totali"] += risultato["optout"]
        esito["replied_totali"] += risultato["replied"]

    logger.info(f"[WA] reply-scan: {esito}")
    return esito


async def wa_campaign_supervisor(ctx: dict) -> dict:
    """Nessuna campagna 'running' deve restare senza il suo job di invio.

    "running senza worker" era uno stato che il sistema non sapeva ne'
    rappresentare ne' rilevare, e ci si arrivava da quattro strade diverse
    (review 07/08): l'avvio dalla UI (chiuso a monte, ora `avvia()` accoda), un
    riavvio del PC (la guardia di cold-start in work_enqueue filtra su
    `Campaign`, cioe' solo Instagram), un `POST /wa/ops/resume` dopo il
    kill-switch (il task era uscito senza rischedulare, perche' 'wa_halted' e'
    un motivo terminale per wa_send_task), e un job perso per un blip di Redis.
    Questo cron e' la rete sotto tutte e quattro.

    Non serve ispezionare Redis per sapere se il job c'e': ARQ scarta da solo
    un enqueue con un `_job_id` gia' presente. E' la stessa proprieta' su cui
    conta gia' wa_send_job_id ("un solo job di invio per numero"), qui usata al
    contrario -- si riaccoda e basta, e se il job c'era non succede niente.

    Il filtro "almeno una riga eleggibile ADESSO" non e' un'ottimizzazione: e'
    cio' che impedisce di aprire il browser ogni quindici minuti su una
    campagna che non ha piu' niente da fare, cioe' rumore su un canale il cui
    pacing esiste per non farsi notare. La condizione ricalca la query di
    eleggibilita' del contratto 7.3 (wa_worker.claim_next_wa_contact): se
    cambia li', va cambiata anche qui.
    """
    from datetime import datetime, timedelta

    from loguru import logger
    from sqlalchemy import or_, select

    from app.config import settings
    from app.database import AsyncSessionLocal
    from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                               WaContact, WaContactStatus, WaNumber, WaNumberStatus)
    from app.services import bot_state_service
    from app.workers.wa_worker import enqueue_wa_workers

    esito = {"controllate": 0, "riaccodate": 0}

    if await bot_state_service.is_wa_halted():
        # A canale fermo il job uscirebbe subito con motivo 'wa_halted' senza
        # rischedulare: l'unico effetto sarebbe una riga di log per campagna,
        # ogni quindici minuti, per tutta la durata dell'incidente.
        return esito

    now = datetime.utcnow()
    stale_cutoff = now - timedelta(minutes=int(settings.wa_lock_timeout_min))

    async with AsyncSessionLocal() as db:
        righe = (await db.execute(
            select(WaCampaign.id)
            .join(WaNumber, WaNumber.id == WaCampaign.wa_number_id)
            .join(WaCampaignContact, WaCampaignContact.campaign_id == WaCampaign.id)
            .join(WaContact, WaContact.id == WaCampaignContact.contact_id)
            .where(
                WaCampaign.status == WaCampaignStatus.running,
                WaNumber.status == WaNumberStatus.active,
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
            .distinct()
        )).all()

    for (campaign_id,) in righe:
        esito["controllate"] += 1
        try:
            if await enqueue_wa_workers(campaign_id):
                esito["riaccodate"] += 1
                logger.warning(f"[WA] supervisore: campagna {campaign_id} era "
                               "running senza job di invio, riaccodata")
        except Exception as exc:
            # Un guasto su una campagna non deve fermare le altre: stesso
            # pattern per-elemento gia' in uso in wa_session_healthcheck.
            logger.error(f"[WA] supervisore: riaccodamento di {campaign_id} "
                         f"fallito ({type(exc).__name__})")

    return esito


async def wa_deadman_ping(ctx: dict) -> dict:
    """Dead-man's switch esterno (review P6, 07/08): se il backend/cron gira,
    manda un ping a un servizio esterno che aspetta di sentirsi contattato a
    intervalli regolari. Se il PC si spegne, il ping smette di arrivare e
    l'allarme parte DAL SERVIZIO ESTERNO, non da questo processo -- e' l'unico
    modo che un health-check interno non puo' dare: un processo morto non puo'
    avvisare che e' morto.

    wa_deadman_ping_url vuoto = disabilitato (fail-safe: nessun URL
    configurato non deve fallire ne' loggare rumore a ogni giro)."""
    from loguru import logger

    url = (settings.wa_deadman_ping_url or "").strip()
    if not url:
        logger.debug("[WA] dead-man's switch: WA_DEADMAN_PING_URL non "
                     "configurato, ping saltato")
        return {"inviato": False, "motivo": "non_configurato"}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        return {"inviato": True}
    except Exception as exc:
        # Un ping fallito NON deve fermare il cron worker (e' proprio
        # l'assenza di ping, rilevata dal servizio esterno, il segnale che
        # conta): si logga e basta, il prossimo giro riprova.
        logger.warning(f"[WA] dead-man's switch: ping fallito "
                       f"({type(exc).__name__}: {exc})")
        return {"inviato": False, "motivo": type(exc).__name__}


async def release_stale_wa_profile_locks(ctx: dict) -> dict:
    """Pulizia proattiva di wa:profile-lock:* (wa_profile_lock.release_stale),
    a tutte le ore -- a differenza di wa_session_healthcheck, non apre
    nessun browser e non compete per il lock stesso, quindi non ha motivo
    di stare dentro la finestra attiva: un worker puo' morire alle 3 di
    notte e lasciare il canale bloccato fino a 90 min senza che nessuno se
    ne accorga prima delle 9. Rationale della soglia nel docstring di
    release_stale in wa_profile_lock.py."""
    from loguru import logger

    from app.services import wa_profile_lock

    rilasciati = await wa_profile_lock.release_stale()
    if rilasciati:
        logger.info(f"[WA] cron lock stale: {rilasciati} lock profilo rilasciati")
    return {"lock_profilo_rilasciati": rilasciati}


class CronWorkerSettings:
    functions = []
    cron_jobs = [
        cron(daily_reset, hour=0, minute=5),
        cron(release_stale_locks, minute={0, 15, 30, 45}),
        # Reply-check UNA volta al giorno (era ogni 30 min): la lettura inbox via
        # API e' tracciabile come bot: girarla raramente riduce il footprint/rischio
        # checkpoint. Le risposte vengono comunque rilevate (marcate 'replied' in
        # modo permanente al primo passaggio). Ambito ristretto: solo campagne attive
        # + invii recenti (vedi reply_checker + reply_check_max_age_days).
        cron(check_replies, hour={13}, minute={0}),
        cron(recover_sending, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
        cron(telegram_commands, minute=set(range(60))),
        cron(wa_session_healthcheck, minute={0, 30}, hour=set(range(9, 20))),
        cron(wa_reply_scan, minute={15, 45}, hour=set(range(9, 20))),
        # Sfasato rispetto a health-check (0/30) e reply-scan (15/45): i tre
        # cron WA competono per lo stesso lucchetto profilo, e farli partire
        # insieme significherebbe che due su tre trovano occupato e saltano.
        #
        # A TUTTE le ore, a differenza degli altri due: questo cron non apre
        # nessun browser (accoda e basta, e il worker ha i suoi cancelli di
        # finestra oraria), mentre l'invariante che protegge -- "nessuna
        # campagna running senza worker" -- non e' limitata alle ore attive.
        # Con la finestra 9-19, una campagna avviata alle 20:15 il cui enqueue
        # fallisce per un blip di Redis restava senza worker fino alle 9:10 del
        # giorno dopo, in silenzio, perche' _accoda_worker ingoia l'errore
        # proprio contando su questo cron. E `active_hours` e' anche un campo
        # per-campagna: un tenant con orari diversi sarebbe rimasto del tutto
        # fuori dalla rete.
        cron(wa_campaign_supervisor, minute={10, 25, 40, 55}),
        # Sfasato dagli altri tre cron WA (0/30, 15/45, 10/25/40/55): non
        # apre browser ne' tocca il lock, quindi non c'e' motivo di
        # allinearlo, ma restare fuori dai loro minuti rende i log piu'
        # facili da leggere in sequenza.
        cron(release_stale_wa_profile_locks, minute={5, 20, 35, 50}),
        # Ogni 5 min, a tutte le ore: il dead-man's switch deve sapere che il
        # processo e' vivo anche fuori dalla finestra attiva del canale --
        # se il PC si spegne alle 3 di notte, l'allarme deve partire quando
        # succede, non aspettare le 9.
        cron(wa_deadman_ping, minute=set(range(0, 60, 5))),
    ]
    queue_name = ARQ_CRON_QUEUE
    redis_settings = arq_redis_settings()
    max_jobs = 5
    # NON impostare keep_result=0: arq usa la persistenza del result key per il
    # dedup dei tick cron. A 0 ogni poll ri-accoda il tick "mancato" → loop.
    # Default arq (3600s) = dedup corretto.
