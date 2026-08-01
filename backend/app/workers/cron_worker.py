"""Dedicated ARQ worker for cron jobs.

Run: arq app.workers.cron_worker.CronWorkerSettings
"""
from arq import cron

from app.services.work_enqueue import ARQ_CRON_QUEUE, arq_redis_settings
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
    dedicato e non dentro il worker di invio.
    """
    from app.database import AsyncSessionLocal
    from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                               WaNumber, WaNumberStatus)
    from app.services import notifier, wa_number_manager
    from app.config import settings
    from datetime import datetime, timedelta
    from loguru import logger
    from sqlalchemy import select, update

    esito = {"controllati": 0, "caduti": 0, "cooldown_rilasciati": 0, "lock_rilasciati": 0}

    async with AsyncSessionLocal() as db:
        numeri = (await db.execute(
            select(WaNumber).where(WaNumber.status.notin_([
                WaNumberStatus.retired, WaNumberStatus.suspended,
                WaNumberStatus.pending_qr]))
        )).scalars().all()
        ids = [n.id for n in numeri]

    for number_id in ids:
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
    ]
    queue_name = ARQ_CRON_QUEUE
    redis_settings = arq_redis_settings()
    max_jobs = 5
    # NON impostare keep_result=0: arq usa la persistenza del result key per il
    # dedup dei tick cron. A 0 ogni poll ri-accoda il tick "mancato" → loop.
    # Default arq (3600s) = dedup corretto.
