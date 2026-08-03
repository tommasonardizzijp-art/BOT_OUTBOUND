"""Dedicated ARQ worker for cron jobs.

Run: arq app.workers.cron_worker.CronWorkerSettings
"""
from arq import cron

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

    esito["cooldown_rilasciati"] = len(await wa_number_manager.release_expired_wa_cooldowns())

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
    ]
    queue_name = ARQ_CRON_QUEUE
    redis_settings = arq_redis_settings()
    max_jobs = 5
    # NON impostare keep_result=0: arq usa la persistenza del result key per il
    # dedup dei tick cron. A 0 ogni poll ri-accoda il tick "mancato" → loop.
    # Default arq (3600s) = dedup corretto.
