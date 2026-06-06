"""ARQ worker configuration."""
from arq.worker import func
from app.services.work_enqueue import ARQ_MAIN_QUEUE, arq_redis_settings
from app.workers.scrape_worker import scrape_followers_task
from app.workers.message_worker import run_campaign_task
from app.workers.import_worker import resolve_imports_task


async def pre_generate_messages_task(ctx: dict, campaign_id: str) -> None:
    """
    ARQ task: pre-generate AI messages.
    - If require_approval: generates only N preview messages → pending_approval.
      Full batch starts only after user approves via /approve-preview endpoint.
    - Otherwise: generates all messages in one shot (legacy behavior).
    """
    from app.services.ai_personalizer import generate_messages_batch, generate_preview_batch
    from app.utils.events import emit as emit_event
    from app.database import AsyncSessionLocal
    from app.models.campaign import Campaign
    from app.models.follower import Follower, FollowerStatus
    from sqlalchemy import select, func
    from loguru import logger

    logger.info(f"[PreGen] Starting for campaign {campaign_id}")
    emit_event(campaign_id, "pregen_started", "Pre-generazione avviata...")

    # Load campaign to check require_approval
    async with AsyncSessionLocal() as db:
        camp = (await db.execute(select(Campaign).where(Campaign.id == campaign_id))).scalar_one_or_none()
        if not camp:
            return

        eligible = await db.scalar(
            select(func.count(Follower.id)).where(
                Follower.campaign_id == campaign_id,
                Follower.status == FollowerStatus.bio_scraped,
            )
        ) or 0

    if eligible == 0:
        emit_event(campaign_id, "pregen_completed", "Nessun follower in stato bio_scraped — niente da generare", level="warn")
        logger.info(f"[PreGen] No eligible followers for campaign {campaign_id}")
        return

    emit_event(campaign_id, "pregen_progress", f"Trovati {eligible} follower — genero anteprima di {camp.approval_sample_size} messaggi...")

    if camp.require_approval:
        # Preview-first flow: generate only N sample messages
        try:
            count = await generate_preview_batch(campaign_id, camp.approval_sample_size or 5)
            if count > 0:
                emit_event(campaign_id, "preview_ready",
                           f"Anteprima pronta: {count} messaggi generati. Approva per continuare o rigenera.")
            else:
                emit_event(campaign_id, "pregen_completed", "Nessun follower disponibile per l'anteprima", level="warn")
            logger.info(f"[PreGen] Preview done: {count} messages for campaign {campaign_id}")
        except Exception as e:
            emit_event(campaign_id, "pregen_error", f"Errore anteprima: {str(e)[:120]}", level="error")
            logger.error(f"[PreGen] Preview failed for campaign {campaign_id}: {e}")
            raise
    else:
        # Legacy full-batch flow (no approval required)
        try:
            count = await generate_messages_batch(campaign_id)
            emit_event(campaign_id, "pregen_completed", f"Pre-generazione completata: {count} messaggi generati")
            logger.info(f"[PreGen] Full batch done: {count} messages for campaign {campaign_id}")
        except Exception as e:
            emit_event(campaign_id, "pregen_error", f"Errore pre-generazione: {str(e)[:120]}", level="error")
            logger.error(f"[PreGen] Failed for campaign {campaign_id}: {e}")
            raise


async def full_batch_generate_task(ctx: dict, campaign_id: str) -> None:
    """ARQ task: generate all remaining bio_scraped messages after preview approval."""
    from app.services.ai_personalizer import generate_messages_batch
    from app.utils.events import emit as emit_event
    from loguru import logger

    logger.info(f"[FullBatch] Starting for campaign {campaign_id}")
    emit_event(campaign_id, "pregen_progress", "Generazione batch completo in corso...")
    try:
        count = await generate_messages_batch(campaign_id)
        emit_event(campaign_id, "pregen_completed", f"Batch completo: {count} messaggi generati")
        logger.info(f"[FullBatch] Done: {count} messages for campaign {campaign_id}")
    except Exception as e:
        emit_event(campaign_id, "pregen_error", f"Errore batch: {str(e)[:120]}", level="error")
        logger.error(f"[FullBatch] Failed for campaign {campaign_id}: {e}")
        raise


async def daily_reset(ctx: dict) -> None:
    """
    Cron (daily at midnight UTC):
    1. Reset daily_message_count and scrape_lookups_today for all accounts.
    2. Re-activate accounts whose cooldown has expired.
    3. Advance warmup_day for accounts in warm-up.
    4. Restart workers for campaigns still in 'running' state
       (workers exit when they hit daily limits; this brings them back next day).
    """
    from app.database import AsyncSessionLocal
    from app.models.account import InstagramAccount, AccountStatus
    from app.models.campaign import Campaign, CampaignStatus
    from app.services.work_enqueue import enqueue_dm_workers_with_redis
    from sqlalchemy import select, update
    from datetime import datetime
    from loguru import logger

    logger.info("[Cron] daily_reset: starting...")
    async with AsyncSessionLocal() as db:
        # ── Reset daily message counters ──────────────────────────────────
        await db.execute(update(InstagramAccount).values(daily_message_count=0, scrape_lookups_today=0))

        # ── Re-activate expired cooldowns ──────────────────────────────────
        await db.execute(
            update(InstagramAccount)
            .where(
                InstagramAccount.status == AccountStatus.cooldown,
                InstagramAccount.cooldown_until <= datetime.utcnow(),
            )
            .values(status=AccountStatus.active, cooldown_until=None)
        )
        await db.commit()

        # ── Advance warmup_day (idempotent — uses warmup_advanced_date guard) ──
        from app.services.account_manager import advance_warmup_if_needed
        await advance_warmup_if_needed()
        logger.info("[Cron] Warmup advancement check done")

        from app.services.bot_state_service import is_halted
        if await is_halted(db):
            logger.warning("[Cron] daily_reset: BOT_HALTED active, skipping worker restart")
            return

        # ── Restart workers for running campaigns ─────────────────────────
        # Workers self-exit when daily limits are hit. The daily cron restarts
        # them so campaigns resume automatically after midnight.
        running = await db.execute(
            select(Campaign).where(
                Campaign.status.in_([
                    CampaignStatus.running,
                    CampaignStatus.scraping_and_running,
                ])
            )
        )
        for campaign in running.scalars().all():
            enqueued = await enqueue_dm_workers_with_redis(ctx["redis"], campaign.id)
            logger.info(
                f"[Cron] Restarted {enqueued} staggered worker(s) for campaign '{campaign.name}'"
            )

    logger.info("[Cron] daily_reset: done")


async def release_stale_locks(ctx: dict) -> None:
    """
    Cron (every 15 minutes):
    1. Release follower locks held by crashed workers (stale > LOCK_TIMEOUT_MINUTES).
    2. Auto-pause campaigns that are still 'running' but have had no activity for 30+ min
       (crash recovery — prevents campaigns from being stuck in running state forever).
    """
    from app.database import AsyncSessionLocal
    from app.services.campaign_orchestrator import release_stale_locks as _release
    from app.models.campaign import Campaign, CampaignStatus
    from app.models.follower import Follower
    from sqlalchemy import select, func
    from datetime import datetime, timedelta
    from loguru import logger

    async with AsyncSessionLocal() as db:
        await _release(db)

        # Auto-pause crashed campaigns. Soglia > max pausa sessione legittima:
        # i worker short-lived si ri-accodano con defer fino a SESSION_BREAK_MAX
        # min, altrimenti un defer normale verrebbe scambiato per crash.
        from app.config import settings as _settings
        _idle_min = max(120, _settings.session_break_max_minutes + 45)
        crash_cutoff = datetime.utcnow() - timedelta(minutes=_idle_min)
        running_result = await db.execute(
            select(Campaign).where(
                Campaign.status == CampaignStatus.running,
                Campaign.updated_at < crash_cutoff,
            )
        )
        paused_count = 0
        for campaign in running_result.scalars().all():
            locked = await db.scalar(
                select(func.count(Follower.id)).where(
                    Follower.campaign_id == campaign.id,
                    Follower.locked_by_account_id.isnot(None),
                )
            )
            if locked == 0:
                campaign.status = CampaignStatus.paused
                paused_count += 1
                logger.warning(
                    f"[Cron] Auto-paused '{campaign.name}': running but no activity for 30+ min (crash recovery)"
                )
        if paused_count > 0:
            await db.commit()


async def check_replies(ctx: dict) -> None:
    """
    Cron (every 30 minutes): scan DM inbox of all active accounts for replies.
    Updates Follower.status to 'replied' when a response is detected.
    Leads page picks this up automatically via the ig_user_id JOIN.
    """
    from app.services.reply_checker import check_all_replies
    from loguru import logger

    logger.info("[Cron] check_replies: starting scan...")
    try:
        count = await check_all_replies()
        if count > 0:
            logger.info(f"[Cron] check_replies: {count} new replies detected")
        else:
            logger.info("[Cron] check_replies: no new replies found")
    except Exception as e:
        logger.error(f"[Cron] check_replies failed: {e}")


async def recover_sending(ctx: dict) -> None:
    """
    Cron (every 5 minutes): reconcile Message rows stuck in status='sending'.
    These arise when send_dm() crashes after pressing Enter but before the
    'sent' commit. The recovery checker uses instagrapi to confirm delivery.
    """
    from app.services.recovery_checker import recover_sending_messages
    from loguru import logger

    logger.info("[Cron] recover_sending: starting...")
    try:
        counts = await recover_sending_messages()
        if counts["recovered"] > 0 or counts["retried"] > 0:
            logger.info(
                f"[Cron] recover_sending: recovered={counts['recovered']} "
                f"retried={counts['retried']} skipped={counts['skipped']} "
                f"errors={counts['errors']}"
            )
        else:
            logger.debug("[Cron] recover_sending: nothing to do")
    except Exception as e:
        logger.error(f"[Cron] recover_sending failed: {e}")


async def telegram_commands(ctx: dict) -> None:
    """Cron: poll Telegram admin commands (/status, /pause, /resume, /halt, /unhalt, /logs, /anomalies)."""
    from app.services.telegram_commands import poll_telegram_commands
    from loguru import logger

    try:
        processed = await poll_telegram_commands(ctx["redis"])
        if processed:
            logger.info(f"[Cron] telegram_commands: processed={processed}")
    except Exception as e:
        logger.error(f"[Cron] telegram_commands failed: {e}")


async def on_startup(ctx: dict) -> None:
    """Pause work left active before this worker cold-started.

    Redis can still hold queued jobs and the DB can still say ``running`` after
    a terminal or PC shutdown. The operator must explicitly resume work from
    the authenticated control plane.
    """
    from loguru import logger
    from app.services.work_enqueue import pause_active_work_on_startup

    try:
        counts = await pause_active_work_on_startup()
        logger.info(f"[Startup] Cold-start guard applied: {counts}")
    except Exception as e:
        logger.error(f"[Startup] Cold-start guard failed: {e}")


class WorkerSettings:
    functions = [
        scrape_followers_task,
        func(run_campaign_task, max_tries=10000),
        pre_generate_messages_task,
        full_batch_generate_task,
        resolve_imports_task,
    ]
    cron_jobs = []
    queue_name = ARQ_MAIN_QUEUE
    redis_settings = arq_redis_settings()
    on_startup = on_startup
    max_jobs = 10
    job_timeout = 3600      # short-lived batches; anything longer is stuck
    keep_result = 0          # don't cache job keys after completion — prevents dedup blocking on resume
