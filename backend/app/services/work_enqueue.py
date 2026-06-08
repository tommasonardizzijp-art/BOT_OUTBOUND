"""Shared ARQ enqueue helpers for campaign work."""
import json
import random
from datetime import datetime, timedelta
from urllib.parse import urlparse

from arq.connections import RedisSettings
from loguru import logger
from sqlalchemy import func, select, update

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.account import AccountStatus, InstagramAccount
from app.models.activity_log import ActivityLog
from app.models.campaign import Campaign, CampaignStatus
from app.models.campaign_account import CampaignAccount
from app.models.follower import Follower
from app.models.message import Message, MessageStatus
from app.services.notifier import send_campaign_auto_pause_alert
from app.utils.events import emit as emit_event


ARQ_MAIN_QUEUE = "arq:queue"
ARQ_CRON_QUEUE = "arq:cron"
DM_STARTUP_STAGGER_MAX_SECONDS = 5 * 60
# A restart should not pause a campaign while a worker is still legitimately
# active. Inter-message delays are up to 8 min and session-break heartbeats
# happen every 10 min, so 20 min keeps us above normal idle windows.
STARTUP_ACTIVE_WORK_GRACE_SECONDS = max(20 * 60, settings.max_delay_seconds + 5 * 60)


def dm_worker_job_id(campaign_id: str, account_id: str) -> str:
    return f"worker:{campaign_id}:{account_id}"


def dm_worker_redis_keys(campaign_id: str, account_id: str) -> tuple[str, str, str]:
    job_id = dm_worker_job_id(campaign_id, account_id)
    return (
        f"arq:job:{job_id}",
        f"arq:retry:{job_id}",
        f"arq:in-progress:{job_id}",
    )


def arq_redis_settings() -> RedisSettings:
    from app.config import settings

    parsed = urlparse(settings.redis_url)
    database = int((parsed.path or "/0").lstrip("/") or "0")
    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        database=database,
        username=parsed.username,
        password=parsed.password,
        ssl=parsed.scheme == "rediss",
        conn_timeout=30,
        conn_retries=10,
        conn_retry_delay=2,
    )


async def _enqueue_scrape_with_redis(redis, campaign_id: str) -> bool:
    job_id = f"scrape:{campaign_id}"
    await redis.delete(
        f"arq:job:{job_id}",
        f"arq:retry:{job_id}",
        f"arq:in-progress:{job_id}",
    )
    await redis.enqueue_job(
        "scrape_followers_task",
        campaign_id,
        _job_id=job_id,
        _queue_name=ARQ_MAIN_QUEUE,
    )
    return True


async def _enqueue_list_with_redis(redis, campaign_id: str) -> bool:
    job_id = f"list:{campaign_id}"
    await redis.delete(
        f"arq:job:{job_id}",
        f"arq:retry:{job_id}",
        f"arq:in-progress:{job_id}",
    )
    await redis.enqueue_job(
        "list_followers_task",
        campaign_id,
        _job_id=job_id,
        _queue_name=ARQ_MAIN_QUEUE,
    )
    return True


async def _enqueue_bios_with_redis(redis, campaign_id: str) -> bool:
    job_id = f"bios:{campaign_id}"
    await redis.delete(
        f"arq:job:{job_id}",
        f"arq:retry:{job_id}",
        f"arq:in-progress:{job_id}",
    )
    await redis.enqueue_job(
        "scrape_bios_task",
        campaign_id,
        _job_id=job_id,
        _queue_name=ARQ_MAIN_QUEUE,
    )
    return True


async def enqueue_list(campaign_id: str) -> bool:
    import arq
    redis = await arq.create_pool(arq_redis_settings())
    try:
        return await _enqueue_list_with_redis(redis, campaign_id)
    finally:
        await redis.aclose()


async def enqueue_bios(campaign_id: str) -> bool:
    import arq
    redis = await arq.create_pool(arq_redis_settings())
    try:
        return await _enqueue_bios_with_redis(redis, campaign_id)
    finally:
        await redis.aclose()


async def _enqueue_dm_workers_with_redis(redis, campaign_id: str) -> int:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(CampaignAccount, InstagramAccount.username)
            .join(InstagramAccount, InstagramAccount.id == CampaignAccount.account_id)
            .where(
                CampaignAccount.campaign_id == campaign_id,
                CampaignAccount.is_active == True,
                CampaignAccount.role.in_(("dm", "both")),
                InstagramAccount.status.in_((AccountStatus.active, AccountStatus.warming_up)),
            )
        )
        campaign_accounts = result.all()

    if not campaign_accounts:
        logger.warning(f"[Enqueue] No usable DM accounts for campaign {campaign_id}")
        return 0

    enqueued = 0
    from app.utils.events import emit as emit_event

    for ca, account_username in campaign_accounts:
        job_id = dm_worker_job_id(campaign_id, ca.account_id)
        defer_seconds = dm_startup_stagger_seconds()
        await redis.delete(*dm_worker_redis_keys(campaign_id, ca.account_id))
        await redis.enqueue_job(
            "run_campaign_task",
            campaign_id,
            ca.account_id,
            _job_id=job_id,
            _defer_by=defer_seconds,
            _queue_name=ARQ_MAIN_QUEUE,
        )
        logger.info(
            f"[Enqueue] DM worker deferred {defer_seconds}s for "
            f"campaign={campaign_id}, account=@{account_username} ({ca.account_id})"
        )
        emit_event(
            campaign_id,
            "worker_queued",
            _dm_worker_queued_detail(account_username, defer_seconds),
        )
        enqueued += 1
    return enqueued


def dm_startup_stagger_seconds() -> int:
    """Randomize DM worker start time without sleeping inside an ARQ job."""
    return random.randint(0, DM_STARTUP_STAGGER_MAX_SECONDS)


def _dm_worker_queued_detail(account_username: str, defer_seconds: int) -> str:
    if defer_seconds < 60:
        return f"Worker DM accodato per @{account_username}: avvio entro 1 min."

    defer_minutes = (defer_seconds + 59) // 60
    return f"Worker DM accodato per @{account_username}: avvio tra circa {defer_minutes} min."


async def _campaign_has_inflight_work(db, campaign_id: str) -> dict[str, int]:
    """Return a small snapshot of work that is still in-flight for a campaign."""
    sending = await db.scalar(
        select(func.count(Message.id)).where(
            Message.campaign_id == campaign_id,
            Message.status == MessageStatus.sending,
        )
    ) or 0
    locked = await db.scalar(
        select(func.count(Follower.id)).where(
            Follower.campaign_id == campaign_id,
            Follower.locked_by_account_id.isnot(None),
        )
    ) or 0
    return {"sending": int(sending), "locked": int(locked)}


async def enqueue_dm_workers_with_redis(redis, campaign_id: str) -> int:
    """Queue one staggered DM worker per usable campaign account."""
    return await _enqueue_dm_workers_with_redis(redis, campaign_id)


async def pause_active_work_on_startup() -> dict[str, int]:
    """Pause stale active work after a DM worker cold-start.

    A start/resume can race the worker startup while staggered ARQ jobs are being
    enqueued. Keep recently touched campaigns alive; pause only active states
    inherited from a previous process/session.
    """
    counts = {"campaigns_paused": 0, "locks_released": 0}
    active_statuses = (
        CampaignStatus.running,
        CampaignStatus.scraping,
        CampaignStatus.scraping_and_running,
        CampaignStatus.scraping_break,
        CampaignStatus.listing,
        CampaignStatus.listing_break,
    )
    stale_before = datetime.utcnow() - timedelta(seconds=STARTUP_ACTIVE_WORK_GRACE_SECONDS)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Campaign).where(
                Campaign.status.in_(active_statuses),
                Campaign.updated_at < stale_before,
            )
        )
        campaigns = list(result.scalars().all())
        campaign_ids: list[str] = []
        auto_pause_alerts: list[dict] = []
        now = datetime.utcnow()

        for campaign in campaigns:
            previous_status = campaign.status.value
            previous_updated_at = campaign.updated_at
            inflight = await _campaign_has_inflight_work(db, campaign.id)

            # If there is still evidence of a live worker session, do not force
            # the campaign into paused: let recovery reconcile the in-flight work.
            if inflight["sending"] > 0 or inflight["locked"] > 0:
                emit_event(
                    campaign.id,
                    "startup_guard_skipped",
                    (
                        f"Controllo riavvio saltato per {campaign.name}: "
                        f"lavoro ancora in corso (sending={inflight['sending']}, locked={inflight['locked']})."
                    ),
                    level="warn",
                )
                logger.warning(
                    f"[Startup] Skipping auto-pause for '{campaign.name}' "
                    f"(in-flight sending={inflight['sending']}, locked={inflight['locked']})"
                )
                continue

            campaign.status = CampaignStatus.paused
            campaign.scrape_break_until = None
            campaign.scrape_break_prev_status = None
            campaign.updated_at = now
            campaign_ids.append(campaign.id)
            db.add(
                ActivityLog(
                    campaign_id=campaign.id,
                    action="campaign_auto_paused",
                    details=json.dumps(
                        {
                            "reason": "worker_startup_requires_operator_resume",
                            "previous_status": previous_status,
                            "stale_before": stale_before.isoformat(),
                            "campaign_updated_at": previous_updated_at.isoformat() if previous_updated_at else None,
                            "inflight": inflight,
                        }
                    ),
                )
            )
            emit_event(
                campaign.id,
                "campaign_auto_paused",
                (
                    f"Campagna messa in pausa al riavvio: "
                    f"stato precedente={previous_status}, sending={inflight['sending']}, locked={inflight['locked']}."
                ),
                level="warn",
            )
            auto_pause_alerts.append(
                {
                    "campaign_name": campaign.name,
                    "campaign_id": campaign.id,
                    "reason": "worker_startup_requires_operator_resume",
                    "level": "warning",
                    "details": {
                        "previous_status": previous_status,
                        "inflight": inflight,
                    },
                }
            )

        if campaign_ids:
            released = await db.execute(
                update(Follower)
                .where(
                    Follower.campaign_id.in_(campaign_ids),
                    Follower.locked_by_account_id.isnot(None),
                )
                .values(locked_by_account_id=None, locked_at=None)
            )
            counts["campaigns_paused"] = len(campaign_ids)
            counts["locks_released"] = released.rowcount or 0
            await db.commit()
            for alert in auto_pause_alerts:
                await send_campaign_auto_pause_alert(**alert)

    if counts["campaigns_paused"]:
        logger.warning(
            "[Startup] Active campaign work paused until explicit operator resume: "
            f"{counts}"
        )
    else:
        logger.info("[Startup] No active campaign work to pause")
    return counts


async def reenqueue_one_dm_worker(campaign_id: str, account_id: str, defer_seconds: int) -> None:
    import arq

    redis = await arq.create_pool(arq_redis_settings())
    try:
        job_id = dm_worker_job_id(campaign_id, account_id)
        await redis.delete(f"arq:job:{job_id}", f"arq:retry:{job_id}")
        await redis.enqueue_job(
            "run_campaign_task",
            campaign_id,
            account_id,
            _job_id=job_id,
            _defer_by=defer_seconds,
            _queue_name=ARQ_MAIN_QUEUE,
        )
    finally:
        await redis.aclose()


async def dm_worker_job_exists(campaign_id: str, account_id: str) -> bool:
    import arq

    redis = await arq.create_pool(arq_redis_settings())
    try:
        return bool(await redis.exists(*dm_worker_redis_keys(campaign_id, account_id)))
    finally:
        await redis.aclose()


async def enqueue_scrape(campaign_id: str) -> bool:
    import arq

    redis = await arq.create_pool(arq_redis_settings())
    try:
        return await _enqueue_scrape_with_redis(redis, campaign_id)
    finally:
        await redis.aclose()


async def _enqueue_resolve_with_redis(redis, campaign_id: str) -> bool:
    job_id = f"resolve:{campaign_id}"
    await redis.delete(
        f"arq:job:{job_id}",
        f"arq:retry:{job_id}",
        f"arq:in-progress:{job_id}",
    )
    await redis.enqueue_job(
        "resolve_imports_task",
        campaign_id,
        _job_id=job_id,
        _queue_name=ARQ_MAIN_QUEUE,
    )
    return True


async def enqueue_resolve(campaign_id: str) -> bool:
    """Enqueue the import-resolution job (dedup by job id, mirrors enqueue_scrape)."""
    import arq

    redis = await arq.create_pool(arq_redis_settings())
    try:
        return await _enqueue_resolve_with_redis(redis, campaign_id)
    finally:
        await redis.aclose()


async def enqueue_lead_qualification(run_id: str) -> bool:
    """Enqueue a lead qualification batch run."""
    import arq

    redis = await arq.create_pool(arq_redis_settings())
    try:
        job_id = f"lead-qualification:{run_id}"
        await redis.delete(
            f"arq:job:{job_id}",
            f"arq:retry:{job_id}",
            f"arq:in-progress:{job_id}",
        )
        await redis.enqueue_job(
            "qualify_leads_task",
            run_id,
            _job_id=job_id,
            _queue_name=ARQ_MAIN_QUEUE,
        )
        return True
    finally:
        await redis.aclose()


async def _enqueue_collection_with_redis(redis, campaign_id: str, source_type: str) -> bool:
    """Enqueue the right profile-collection job for a campaign.

    Import campaigns resolve a user-provided list (resolve_imports_task); scrape
    campaigns crawl a target page (scrape_followers_task). Centralized so resume,
    /unhalt and boot-recovery never run the scraper on an import campaign (which
    has target_username=None and would fail).
    """
    if source_type == "import":
        return await _enqueue_resolve_with_redis(redis, campaign_id)
    return await _enqueue_scrape_with_redis(redis, campaign_id)


async def enqueue_collection(campaign_id: str, source_type: str) -> bool:
    """Pool wrapper for _enqueue_collection_with_redis."""
    import arq

    redis = await arq.create_pool(arq_redis_settings())
    try:
        return await _enqueue_collection_with_redis(redis, campaign_id, source_type)
    finally:
        await redis.aclose()


async def enqueue_campaign_run(campaign_id: str) -> int:
    import arq

    redis = await arq.create_pool(arq_redis_settings())
    try:
        return await _enqueue_dm_workers_with_redis(redis, campaign_id)
    finally:
        await redis.aclose()


async def reenqueue_active_work() -> dict[str, int]:
    """Requeue work that should be active after a global resume.

    If a campaign's status implies DM workers should run but none can be enqueued
    (no active dm/both accounts), auto-pause it so the UI reflects reality
    instead of showing a perpetually-idle "running" campaign.
    """
    import arq

    redis = await arq.create_pool(arq_redis_settings())
    counts = {"scrape_jobs": 0, "dm_jobs": 0, "breaks_restored": 0, "auto_paused": 0}
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Campaign).where(
                    Campaign.status.in_(
                        (
                            CampaignStatus.scraping,
                            CampaignStatus.scraping_and_running,
                            CampaignStatus.scraping_break,
                            CampaignStatus.running,
                        )
                    )
                )
            )
            campaigns = result.scalars().all()

            for campaign in campaigns:
                if campaign.status == CampaignStatus.scraping_break:
                    prev = campaign.scrape_break_prev_status or CampaignStatus.scraping.value
                    if prev not in {CampaignStatus.scraping.value, CampaignStatus.scraping_and_running.value}:
                        prev = CampaignStatus.scraping.value
                    campaign.status = CampaignStatus(prev)
                    campaign.scrape_break_until = None
                    campaign.scrape_break_prev_status = None
                    campaign.updated_at = datetime.utcnow()
                    counts["breaks_restored"] += 1

            if counts["breaks_restored"]:
                await db.commit()

            for campaign in campaigns:
                status = campaign.status
                if status in (CampaignStatus.scraping, CampaignStatus.scraping_and_running):
                    await _enqueue_collection_with_redis(redis, campaign.id, campaign.source_type)
                    counts["scrape_jobs"] += 1
                if status in (CampaignStatus.running, CampaignStatus.scraping_and_running):
                    dm_count = await _enqueue_dm_workers_with_redis(redis, campaign.id)
                    counts["dm_jobs"] += dm_count
                    if dm_count == 0:
                        # Auto-pause: a running/parallel campaign without DM workers
                        # would otherwise look active forever with nothing happening.
                        # For scraping_and_running we drop back to scraping so the
                        # scraper keeps going (scrape job already enqueued above).
                        if status == CampaignStatus.scraping_and_running:
                            campaign.status = CampaignStatus.scraping
                            campaign.auto_generate = False
                            logger.warning(
                                f"[Enqueue] Campaign {campaign.id} downgraded to scraping: "
                                f"no active DM/both account for parallel mode"
                            )
                        else:
                            campaign.status = CampaignStatus.paused
                            logger.warning(
                                f"[Enqueue] Campaign {campaign.id} auto-paused: "
                                f"no active DM/both account"
                            )
                        campaign.updated_at = datetime.utcnow()
                        counts["auto_paused"] += 1

            await db.commit()
        logger.info(f"[Enqueue] Re-enqueued active work after resume: {counts}")
        return counts
    finally:
        await redis.aclose()
