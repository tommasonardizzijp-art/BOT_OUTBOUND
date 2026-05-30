import json
from datetime import datetime
from loguru import logger
from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, func
from app.database import get_db
from app.models.account import AccountStatus, InstagramAccount
from app.models.campaign import Campaign, CampaignStatus
from app.models.campaign_account import CampaignAccount
from app.models.follower import Follower, FollowerStatus
from app.models.message import Message, MessageStatus
from app.models.activity_log import ActivityLog
from app.schemas.campaign import CampaignCreate, CampaignUpdate, CampaignResponse
from app.services.campaign_control import (
    CampaignControlError,
    check_redis_reachable,
    ensure_bot_accepts_work,
    has_active_role_account,
    pause_campaign_control,
    resume_campaign_control,
)

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


async def _enrich_campaign(campaign: Campaign, db: AsyncSession, include_today: bool = False) -> CampaignResponse:
    """Build CampaignResponse with live-reconciled counters from Follower.status GROUP BY."""
    counts_result = await db.execute(
        select(Follower.status, func.count(Follower.id))
        .where(Follower.campaign_id == campaign.id)
        .group_by(Follower.status)
    )
    counts: dict = {row[0]: row[1] for row in counts_result.all()}

    sent_today = 0
    if include_today:
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        sent_today = await db.scalar(
            select(func.count(Message.id)).where(
                Message.campaign_id == campaign.id,
                Message.status == MessageStatus.sent,
                Message.sent_at >= today_start,
            )
        ) or 0

    sent_only = counts.get(FollowerStatus.sent, 0)
    replied = counts.get(FollowerStatus.replied, 0)
    sent_total = sent_only + replied
    reply_rate = (replied / sent_total) if sent_total else 0.0

    return CampaignResponse.model_validate(campaign).model_copy(update={
        "total_followers": sum(counts.values()),
        "messages_sent": sent_total,
        "messages_replied": replied,
        "reply_rate": reply_rate,
        "messages_failed": counts.get(FollowerStatus.failed, 0),
        "messages_skipped": counts.get(FollowerStatus.skipped, 0),
        "messages_pending": (
            counts.get(FollowerStatus.pending, 0)
            + counts.get(FollowerStatus.bio_scraped, 0)
            + counts.get(FollowerStatus.message_generated, 0)
            + counts.get(FollowerStatus.pending_approval, 0)
        ),
        "messages_sent_today": sent_today,
    })


@router.get("", response_model=list[CampaignResponse])
async def list_campaigns(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Campaign).order_by(Campaign.created_at.desc()))
    campaigns = result.scalars().all()
    if not campaigns:
        return []

    campaign_ids = [c.id for c in campaigns]

    # Batch live counters: one GROUP BY (campaign_id, follower.status) for ALL campaigns.
    # Replaces stale denormalized Campaign.messages_sent/failed/skipped/pending counters
    # so the list page matches what the detail page shows (single source of truth = Follower.status).
    counts_result = await db.execute(
        select(Follower.campaign_id, Follower.status, func.count(Follower.id))
        .where(Follower.campaign_id.in_(campaign_ids))
        .group_by(Follower.campaign_id, Follower.status)
    )
    # status_by_campaign[campaign_id][status] = count
    status_by_campaign: dict[str, dict] = {}
    for cid, status, cnt in counts_result.all():
        status_by_campaign.setdefault(cid, {})[status] = cnt

    enriched: list[CampaignResponse] = []
    for c in campaigns:
        s = status_by_campaign.get(c.id, {})
        total = sum(s.values())
        sent_only = s.get(FollowerStatus.sent, 0)
        replied = s.get(FollowerStatus.replied, 0)
        sent = sent_only + replied
        reply_rate = (replied / sent) if sent else 0.0
        failed = s.get(FollowerStatus.failed, 0)
        skipped = s.get(FollowerStatus.skipped, 0)
        pending = (
            s.get(FollowerStatus.pending, 0)
            + s.get(FollowerStatus.bio_scraped, 0)
            + s.get(FollowerStatus.message_generated, 0)
            + s.get(FollowerStatus.pending_approval, 0)
        )
        enriched.append(CampaignResponse.model_validate(c).model_copy(update={
            "total_followers": total,
            "messages_sent": sent,
            "messages_replied": replied,
            "reply_rate": reply_rate,
            "messages_failed": failed,
            "messages_skipped": skipped,
            "messages_pending": pending,
        }))
    return enriched


@router.post("", response_model=CampaignResponse, status_code=status.HTTP_201_CREATED)
async def create_campaign(data: CampaignCreate, db: AsyncSession = Depends(get_db)):
    campaign = Campaign(
        name=data.name,
        target_username=(data.target_username.lstrip("@") if data.target_username else None),
        source_type=data.source_type,
        base_message_template=data.base_message_template,
        ai_prompt_context=data.ai_prompt_context,
        message_template_b=data.message_template_b,
        daily_limit=data.daily_limit,
        require_approval=data.require_approval,
        approval_sample_size=data.approval_sample_size,
        scrape_mode=data.scrape_mode,
        scrape_session_size=data.scrape_session_size,
        scrape_break_minutes_min=data.scrape_break_minutes_min,
        scrape_break_minutes_max=data.scrape_break_minutes_max,
        bio_fetch_delay_min=data.bio_fetch_delay_min,
        bio_fetch_delay_max=data.bio_fetch_delay_max,
        status=CampaignStatus.draft,
    )
    if campaign.scrape_break_minutes_min > campaign.scrape_break_minutes_max:
        raise HTTPException(status_code=400, detail="scrape_break_minutes_min > max")
    if campaign.bio_fetch_delay_min > campaign.bio_fetch_delay_max:
        raise HTTPException(status_code=400, detail="bio_fetch_delay_min > max")
    db.add(campaign)

    log = ActivityLog(campaign_id=campaign.id, action="campaign_created", details=json.dumps({"name": data.name}))
    db.add(log)

    await db.commit()
    await db.refresh(campaign)
    return await _enrich_campaign(campaign, db)


@router.get("/{campaign_id}", response_model=CampaignResponse)
async def get_campaign(campaign_id: str, db: AsyncSession = Depends(get_db)):
    campaign = await _get_or_404(campaign_id, db)
    return await _enrich_campaign(campaign, db, include_today=True)


@router.put("/{campaign_id}", response_model=CampaignResponse)
async def update_campaign(campaign_id: str, data: CampaignUpdate, db: AsyncSession = Depends(get_db)):
    campaign = await _get_or_404(campaign_id, db)

    # daily_limit can be changed at any time (workers check it live on each iteration)
    if "daily_limit" in data.model_fields_set:
        campaign.daily_limit = data.daily_limit

    # Other fields require draft/ready/paused state
    other_fields = data.model_fields_set - {"daily_limit"}
    if other_fields and campaign.status not in (CampaignStatus.draft, CampaignStatus.ready, CampaignStatus.paused):
        raise HTTPException(status_code=400, detail="Only draft/ready/paused campaigns can have name/template/context updated")

    if data.name is not None:
        campaign.name = data.name
    if data.base_message_template is not None:
        campaign.base_message_template = data.base_message_template
    if data.ai_prompt_context is not None:
        campaign.ai_prompt_context = data.ai_prompt_context
    if "message_template_b" in data.model_fields_set:
        campaign.message_template_b = data.message_template_b
    if data.require_approval is not None:
        campaign.require_approval = data.require_approval
    if data.approval_sample_size is not None:
        campaign.approval_sample_size = data.approval_sample_size
    if data.scrape_mode is not None:
        campaign.scrape_mode = data.scrape_mode
    if data.scrape_session_size is not None:
        campaign.scrape_session_size = data.scrape_session_size
    if data.scrape_break_minutes_min is not None:
        campaign.scrape_break_minutes_min = data.scrape_break_minutes_min
    if data.scrape_break_minutes_max is not None:
        campaign.scrape_break_minutes_max = data.scrape_break_minutes_max
    if data.bio_fetch_delay_min is not None:
        campaign.bio_fetch_delay_min = data.bio_fetch_delay_min
    if data.bio_fetch_delay_max is not None:
        campaign.bio_fetch_delay_max = data.bio_fetch_delay_max

    if campaign.scrape_break_minutes_min > campaign.scrape_break_minutes_max:
        raise HTTPException(status_code=400, detail="scrape_break_minutes_min > max")
    if campaign.bio_fetch_delay_min > campaign.bio_fetch_delay_max:
        raise HTTPException(status_code=400, detail="bio_fetch_delay_min > max")

    campaign.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(campaign)
    return await _enrich_campaign(campaign, db, include_today=True)


@router.delete("/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_campaign(campaign_id: str, db: AsyncSession = Depends(get_db)):
    campaign = await _get_or_404(campaign_id, db)
    if campaign.status in (
        CampaignStatus.running,
        CampaignStatus.scraping,
        CampaignStatus.scraping_and_running,
        CampaignStatus.scraping_break,
    ):
        raise HTTPException(
            status_code=400,
            detail="Metti in pausa la campagna prima di eliminarla (job attivi).",
        )

    # BUG-NEW-21: kill any lingering ARQ worker/scrape/pregen keys for this campaign
    # so they don't block re-use of the same job_ids in future campaigns.
    try:
        import arq
        redis = await arq.create_pool(_arq_redis_settings())
        ca_result = await db.execute(
            select(CampaignAccount).where(CampaignAccount.campaign_id == campaign_id)
        )
        for ca in ca_result.scalars().all():
            job_id = f"worker:{campaign_id}:{ca.account_id}"
            await redis.delete(f"arq:job:{job_id}", f"arq:retry:{job_id}")
        # Also clean up scrape/pregen keys
        for suffix in [f"scrape:{campaign_id}", f"resolve:{campaign_id}", f"pregen:{campaign_id}:preview", f"pregen:{campaign_id}:full"]:
            await redis.delete(f"arq:job:{suffix}", f"arq:retry:{suffix}")
        await redis.aclose()
    except Exception as e:
        logger.warning(f"Could not clean ARQ keys for campaign {campaign_id}: {e}")

    await db.delete(campaign)
    await db.commit()


@router.post("/{campaign_id}/start-scrape", response_model=CampaignResponse)
async def start_scrape(campaign_id: str, db: AsyncSession = Depends(get_db)):
    campaign = await _get_or_404(campaign_id, db)

    if campaign.status not in (CampaignStatus.draft,):
        raise HTTPException(status_code=400, detail="Only draft campaigns can be scraped")

    try:
        await ensure_bot_accepts_work(db)
    except CampaignControlError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if not await has_active_role_account(
        db, campaign_id, ("scraping", "both"), (AccountStatus.active,)
    ):
        raise HTTPException(
            status_code=400,
            detail="Nessun account attivo con ruolo scraping o 'entrambi'. "
            "Assegna un account scraper prima di avviare lo scraping.",
        )

    if not await _check_redis_reachable():
        raise HTTPException(
            status_code=503,
            detail="Redis non raggiungibile. Avviare Redis prima dello scraping.",
        )

    campaign.status = CampaignStatus.scraping
    campaign.updated_at = datetime.utcnow()

    log = ActivityLog(campaign_id=campaign.id, action="scrape_started")
    db.add(log)
    await db.commit()
    await db.refresh(campaign)

    try:
        from app.services.work_enqueue import enqueue_scrape

        await enqueue_scrape(campaign_id)
    except Exception as exc:
        campaign.status = CampaignStatus.draft
        campaign.updated_at = datetime.utcnow()
        db.add(
            ActivityLog(
                campaign_id=campaign.id,
                action="scrape_start_failed",
                details=json.dumps({"reason": "enqueue_failed", "error": str(exc)[:180]}),
            )
        )
        await db.commit()
        raise HTTPException(
            status_code=503,
            detail="Scraping non avviato: impossibile accodare il job. "
            "Controlla Redis e il worker, poi riprova.",
        ) from exc

    return await _enrich_campaign(campaign, db, include_today=True)


@router.post("/{campaign_id}/start", response_model=CampaignResponse)
async def start_campaign(campaign_id: str, db: AsyncSession = Depends(get_db)):
    campaign = await _get_or_404(campaign_id, db)

    if campaign.status not in (CampaignStatus.ready, CampaignStatus.paused):
        raise HTTPException(status_code=400, detail="Campaign must be in ready or paused state to start")

    try:
        await ensure_bot_accepts_work(db)
    except CampaignControlError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # Require at least 1 usable DM/both account — only those produce workers.
    # An account with role='scraping' alone cannot send DMs and would cause
    # the campaign to sit in 'running' with no enqueued workers.
    ca_check = await db.execute(
        select(CampaignAccount)
        .join(InstagramAccount, InstagramAccount.id == CampaignAccount.account_id)
        .where(
            CampaignAccount.campaign_id == campaign_id,
            CampaignAccount.is_active == True,
            CampaignAccount.role.in_(("dm", "both")),
            InstagramAccount.status.in_((AccountStatus.active, AccountStatus.warming_up)),
        )
        .limit(1)
    )
    if not ca_check.scalar_one_or_none():
        raise HTTPException(
            status_code=400,
            detail="Nessun account utilizzabile con ruolo DM o 'entrambi' assegnato a questa campagna. "
                   "Apri il dettaglio della campagna, assegna un account attivo e imposta il ruolo a 'dm' o 'entrambi'."
        )

    # BUG-NEW-12: verify Redis before changing status — avoids campaign stuck in 'running'
    # with no active worker if Redis is down at launch time
    if not await _check_redis_reachable():
        raise HTTPException(
            status_code=503,
            detail="Redis non raggiungibile. Avviare il servizio Redis prima di lanciare la campagna.",
        )

    previous_status = campaign.status
    previous_started_at = campaign.started_at
    campaign.status = CampaignStatus.running
    campaign.started_at = campaign.started_at or datetime.utcnow()
    campaign.updated_at = datetime.utcnow()

    log = ActivityLog(campaign_id=campaign.id, action="campaign_started")
    db.add(log)
    await db.commit()
    await db.refresh(campaign)

    try:
        from app.services.work_enqueue import enqueue_campaign_run

        if await enqueue_campaign_run(campaign_id) == 0:
            raise RuntimeError("zero DM workers enqueued")
    except Exception as exc:
        campaign.status = previous_status
        campaign.started_at = previous_started_at
        campaign.updated_at = datetime.utcnow()
        db.add(
            ActivityLog(
                campaign_id=campaign.id,
                action="campaign_start_failed",
                details=json.dumps({"reason": "enqueue_failed", "error": str(exc)[:180]}),
            )
        )
        await db.commit()
        raise HTTPException(
            status_code=503,
            detail="Campagna non avviata: nessun worker DM accodato. "
            "Controlla Redis, worker e account assegnati, poi riprova.",
        ) from exc

    return await _enrich_campaign(campaign, db, include_today=True)


@router.post("/{campaign_id}/pause", response_model=CampaignResponse)
async def pause_campaign(campaign_id: str, db: AsyncSession = Depends(get_db)):
    try:
        campaign = await pause_campaign_control(db, campaign_id, by="web")
        return await _enrich_campaign(campaign, db, include_today=True)
    except CampaignControlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{campaign_id}/resume", response_model=CampaignResponse)
async def resume_campaign(campaign_id: str, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    try:
        campaign, _counts = await resume_campaign_control(db, campaign_id, by="web", enqueue=True)
        return await _enrich_campaign(campaign, db, include_today=True)
    except CampaignControlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{campaign_id}/stop", response_model=CampaignResponse)
async def stop_campaign(campaign_id: str, db: AsyncSession = Depends(get_db)):
    campaign = await _get_or_404(campaign_id, db)

    if campaign.status not in (
        CampaignStatus.running, CampaignStatus.paused,
        CampaignStatus.scraping, CampaignStatus.scraping_and_running,
        CampaignStatus.scraping_break,
    ):
        raise HTTPException(status_code=400, detail="Campaign is not running, paused or scraping")

    # Stop = paused, not completed — keeps all followers/messages intact so
    # the campaign can be resumed without losing progress.
    campaign.status = CampaignStatus.paused
    campaign.updated_at = datetime.utcnow()

    log = ActivityLog(campaign_id=campaign.id, action="campaign_stopped")
    db.add(log)
    await db.commit()
    await db.refresh(campaign)
    return await _enrich_campaign(campaign, db, include_today=True)


@router.post("/{campaign_id}/reset", response_model=CampaignResponse)
async def reset_campaign(campaign_id: str, db: AsyncSession = Depends(get_db)):
    """Reset a campaign back to draft state so it can be re-scraped."""
    campaign = await _get_or_404(campaign_id, db)

    if campaign.status not in (
        CampaignStatus.error, CampaignStatus.completed, CampaignStatus.paused,
        CampaignStatus.scraping, CampaignStatus.scraping_and_running, CampaignStatus.scraping_break,
    ):
        raise HTTPException(status_code=400, detail="Only error, completed, paused or stuck scraping campaigns can be reset")

    # Count actual followers in DB (they're kept, just status-reset)
    from sqlalchemy import func as sa_func
    actual_count = await db.scalar(
        select(sa_func.count(Follower.id)).where(Follower.campaign_id == campaign_id)
    ) or 0

    campaign.status = CampaignStatus.draft
    campaign.total_followers = actual_count
    campaign.messages_sent = 0
    campaign.messages_failed = 0
    campaign.messages_pending = actual_count
    campaign.scrape_completed_at = None
    campaign.started_at = None
    campaign.completed_at = None
    campaign.auto_generate = False
    campaign.scrape_break_until = None
    campaign.scrape_break_prev_status = None
    campaign.updated_at = datetime.utcnow()

    # BUG-NEW-05: delete old messages so the campaign starts clean
    # Without this, old sent/failed records accumulate and the messages page shows stale data
    await db.execute(delete(Message).where(Message.campaign_id == campaign_id))

    # Reset follower statuses and clear any stale locks
    await db.execute(
        update(Follower)
        .where(Follower.campaign_id == campaign_id)
        .values(status=FollowerStatus.bio_scraped, locked_by_account_id=None, locked_at=None)
    )

    log = ActivityLog(campaign_id=campaign.id, action="campaign_reset")
    db.add(log)
    await db.commit()
    await db.refresh(campaign)
    return await _enrich_campaign(campaign, db, include_today=True)


@router.post("/{campaign_id}/start-dm-auto", response_model=CampaignResponse)
async def start_dm_auto(campaign_id: str, db: AsyncSession = Depends(get_db)):
    """Start DM workers while scraping is still in progress (parallel mode).
    Transitions campaign to scraping_and_running. DM generation happens inline (auto-gen).
    """
    campaign = await _get_or_404(campaign_id, db)

    if campaign.status not in (CampaignStatus.scraping, CampaignStatus.scraping_break):
        raise HTTPException(
            status_code=400,
            detail="Puoi avviare i DM in parallelo solo mentre lo scraping è in corso"
        )

    try:
        await ensure_bot_accepts_work(db)
    except CampaignControlError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # Require at least 1 active dm/both account assigned
    dm_ca = await db.execute(
        select(CampaignAccount)
        .join(InstagramAccount, InstagramAccount.id == CampaignAccount.account_id)
        .where(
            CampaignAccount.campaign_id == campaign_id,
            CampaignAccount.is_active == True,
            CampaignAccount.role.in_(("dm", "both")),
            InstagramAccount.status.in_((AccountStatus.active, AccountStatus.warming_up)),
        )
        .limit(1)
    )
    if not dm_ca.scalar_one_or_none():
        raise HTTPException(
            status_code=400,
            detail="Assegna almeno un account attivo con ruolo 'dm' o 'entrambi' prima di avviare i DM"
        )

    if not await _check_redis_reachable():
        raise HTTPException(
            status_code=503,
            detail="Redis non raggiungibile. Avviare il servizio Redis prima di lanciare la campagna.",
        )

    previous_status = campaign.status
    previous_auto_generate = campaign.auto_generate
    campaign.status = CampaignStatus.scraping_and_running
    campaign.auto_generate = True
    campaign.started_at = campaign.started_at or datetime.utcnow()
    campaign.updated_at = datetime.utcnow()

    log = ActivityLog(campaign_id=campaign.id, action="dm_auto_started",
                      details='{"mode": "parallel"}')
    db.add(log)
    await db.commit()
    await db.refresh(campaign)

    try:
        from app.services.work_enqueue import enqueue_campaign_run

        if await enqueue_campaign_run(campaign_id) == 0:
            raise RuntimeError("zero DM workers enqueued")
    except Exception as exc:
        campaign.status = previous_status
        campaign.auto_generate = previous_auto_generate
        campaign.updated_at = datetime.utcnow()
        db.add(
            ActivityLog(
                campaign_id=campaign.id,
                action="dm_auto_start_failed",
                details=json.dumps({"reason": "enqueue_failed", "error": str(exc)[:180]}),
            )
        )
        await db.commit()
        raise HTTPException(
            status_code=503,
            detail="DM paralleli non avviati: impossibile accodare i worker. "
            "Lo scraping resta nello stato precedente; controlla Redis, worker e account.",
        ) from exc

    return await _enrich_campaign(campaign, db, include_today=True)


@router.post("/{campaign_id}/resume-break", response_model=CampaignResponse)
async def resume_scrape_break(campaign_id: str, db: AsyncSession = Depends(get_db)):
    """Force-resume scraping after a session break without waiting for the timer."""
    campaign = await _get_or_404(campaign_id, db)

    if campaign.status != CampaignStatus.scraping_break:
        raise HTTPException(
            status_code=400,
            detail="La campagna non è in pausa sessione"
        )

    prev = campaign.scrape_break_prev_status or CampaignStatus.scraping.value
    campaign.status = CampaignStatus(prev)
    campaign.scrape_break_until = None
    campaign.scrape_break_prev_status = None
    campaign.updated_at = datetime.utcnow()

    log = ActivityLog(campaign_id=campaign.id, action="scrape_break_resumed")
    db.add(log)
    await db.commit()
    await db.refresh(campaign)
    return await _enrich_campaign(campaign, db, include_today=True)


@router.post("/{campaign_id}/pre-generate", response_model=CampaignResponse)
async def pre_generate_messages(
    campaign_id: str, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)
):
    """Pre-generate AI messages for all bio_scraped followers before starting the campaign.
    M14: decouples Ollama generation from the DM send loop for better throughput.
    """
    campaign = await _get_or_404(campaign_id, db)

    if campaign.status not in (CampaignStatus.ready, CampaignStatus.paused):
        raise HTTPException(
            status_code=400,
            detail="Solo campagne in stato 'ready' o 'paused' possono essere pre-generate",
        )

    background_tasks.add_task(_enqueue_pregen, campaign_id)
    return await _enrich_campaign(campaign, db, include_today=True)


@router.get("/{campaign_id}/ab-stats")
async def get_ab_stats(campaign_id: str, db: AsyncSession = Depends(get_db)):
    """Return A/B testing stats for a campaign: sent/failed counts per template variant.
    M10.
    """
    from app.models.message import Message, MessageStatus
    from sqlalchemy import func, case

    await _get_or_404(campaign_id, db)

    rows = await db.execute(
        select(
            Message.template_variant,
            func.count(Message.id).label("total"),
            func.sum(case((Message.status == MessageStatus.sent, 1), else_=0)).label("sent"),
            func.sum(case((Message.status == MessageStatus.failed, 1), else_=0)).label("failed"),
            func.sum(case((Message.status == MessageStatus.pending, 1), else_=0)).label("pending"),
            func.sum(
                case(
                    (
                        (Message.status == MessageStatus.sent)
                        & (Follower.status == FollowerStatus.replied),
                        1,
                    ),
                    else_=0,
                )
            ).label("replied"),
        )
        .join(Follower, Follower.id == Message.follower_id)
        .where(Message.campaign_id == campaign_id)
        .group_by(Message.template_variant)
    )

    stats: dict = {"variant_a": None, "variant_b": None, "template_b_present": False}
    for row in rows.all():
        variant = row.template_variant or 'a'
        sent = row.sent or 0
        replied = row.replied or 0
        reply_rate = (replied / sent) if sent else 0.0
        data = {
            "total": row.total,
            "sent": sent,
            "failed": row.failed or 0,
            "pending": row.pending or 0,
            "replied": replied,
            "reply_rate": reply_rate,
        }
        if variant == 'a':
            stats["variant_a"] = data
        elif variant == 'b':
            stats["variant_b"] = data
            stats["template_b_present"] = True

    return stats


@router.get("/{campaign_id}/approval-queue")
async def get_approval_queue(campaign_id: str, db: AsyncSession = Depends(get_db)):
    """M15 rev: list followers pending human approval with their generated message."""
    from app.models.message import Message

    await _get_or_404(campaign_id, db)

    # Join followers (pending_approval) with their most recent message
    result = await db.execute(
        select(Follower, Message)
        .join(Message, Message.follower_id == Follower.id, isouter=True)
        .where(
            Follower.campaign_id == campaign_id,
            Follower.status == FollowerStatus.pending_approval,
        )
        .order_by(Follower.created_at.asc())
    )

    items = []
    for follower, message in result.all():
        items.append({
            "follower_id": follower.id,
            "username": follower.username,
            "full_name": follower.full_name,
            "biography": follower.biography,
            "follower_count": follower.follower_count,
            "is_verified": follower.is_verified,
            "message_id": message.id if message else None,
            "generated_text": message.generated_text if message else None,
            "template_variant": message.template_variant if message else None,
        })

    return {"items": items, "total": len(items)}


@router.post("/{campaign_id}/approve-message")
async def approve_message(campaign_id: str, body: dict, db: AsyncSession = Depends(get_db)):
    """M15 rev: approve a message — unblock follower so orchestrator picks it up."""
    follower_id = body.get("follower_id")
    if not follower_id:
        raise HTTPException(status_code=400, detail="follower_id required")

    result = await db.execute(
        select(Follower).where(Follower.id == follower_id, Follower.campaign_id == campaign_id)
    )
    follower = result.scalar_one_or_none()
    if not follower:
        raise HTTPException(status_code=404, detail="Follower not found")
    if follower.status != FollowerStatus.pending_approval:
        raise HTTPException(status_code=400, detail="Follower is not in pending_approval state")

    follower.status = FollowerStatus.message_generated
    follower.updated_at = datetime.utcnow()
    await db.commit()
    return {"ok": True}


@router.post("/{campaign_id}/reject-message")
async def reject_message(campaign_id: str, body: dict, db: AsyncSession = Depends(get_db)):
    """M15 rev: reject a message — delete it and reset follower to bio_scraped for regeneration."""
    follower_id = body.get("follower_id")
    if not follower_id:
        raise HTTPException(status_code=400, detail="follower_id required")

    result = await db.execute(
        select(Follower).where(Follower.id == follower_id, Follower.campaign_id == campaign_id)
    )
    follower = result.scalar_one_or_none()
    if not follower:
        raise HTTPException(status_code=404, detail="Follower not found")

    # Delete the rejected message
    await db.execute(delete(Message).where(Message.follower_id == follower_id))
    # Reset follower so AI regenerates on next pre-gen
    follower.status = FollowerStatus.bio_scraped
    follower.updated_at = datetime.utcnow()
    await db.commit()
    return {"ok": True}


@router.post("/{campaign_id}/approve-preview")
async def approve_preview(campaign_id: str, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """Approve preview batch: move pending_approval followers to message_generated,
    then enqueue full batch generation for remaining bio_scraped followers."""
    from app.utils.events import emit as emit_event

    await _get_or_404(campaign_id, db)

    result = await db.execute(
        update(Follower)
        .where(Follower.campaign_id == campaign_id,
               Follower.status == FollowerStatus.pending_approval)
        .values(status=FollowerStatus.message_generated, updated_at=datetime.utcnow())
    )
    approved = result.rowcount
    await db.commit()

    background_tasks.add_task(_enqueue_full_batch, campaign_id)
    emit_event(campaign_id, "pregen_progress",
               f"Anteprima approvata ({approved} messaggi) — generazione batch completo in coda...")
    return {"ok": True, "approved": approved}


@router.post("/{campaign_id}/reject-preview")
async def reject_preview(campaign_id: str, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """Reject preview batch: delete preview messages, reset followers to bio_scraped,
    then re-enqueue a new preview so user can check the updated prompt."""
    from app.utils.events import emit as emit_event

    await _get_or_404(campaign_id, db)

    result = await db.execute(
        select(Follower).where(Follower.campaign_id == campaign_id,
                               Follower.status == FollowerStatus.pending_approval)
    )
    followers = result.scalars().all()

    for f in followers:
        await db.execute(delete(Message).where(Message.follower_id == f.id))
        f.status = FollowerStatus.bio_scraped
        f.updated_at = datetime.utcnow()

    await db.commit()

    background_tasks.add_task(_enqueue_pregen, campaign_id)
    emit_event(campaign_id, "pregen_progress",
               f"{len(followers)} messaggi annullati — nuova anteprima in generazione...")
    return {"ok": True, "reset": len(followers)}


@router.post("/{campaign_id}/retry-failed", response_model=CampaignResponse)
async def retry_failed(campaign_id: str, db: AsyncSession = Depends(get_db)):
    """Reset all failed followers back to message_generated (or bio_scraped if no message)
    so the orchestrator picks them up again on next run."""
    campaign = await _get_or_404(campaign_id, db)

    if campaign.status not in (CampaignStatus.ready, CampaignStatus.paused, CampaignStatus.running):
        raise HTTPException(status_code=400, detail="Campaign must be ready, paused or running to retry failed messages")

    # Find all failed followers
    failed_result = await db.execute(
        select(Follower).where(
            Follower.campaign_id == campaign_id,
            Follower.status == FollowerStatus.failed,
        )
    )
    failed_followers = failed_result.scalars().all()

    if not failed_followers:
        raise HTTPException(status_code=400, detail="Nessun messaggio fallito da ritentare")

    # Check which ones already have a generated message
    follower_ids = [f.id for f in failed_followers]
    msg_result = await db.execute(
        select(Message.follower_id).where(
            Message.follower_id.in_(follower_ids),
            Message.status == MessageStatus.failed,
        )
    )
    has_message = {row[0] for row in msg_result.all()}

    retry_count = 0
    for follower in failed_followers:
        # Reset message record so it can be retried
        await db.execute(
            update(Message)
            .where(Message.follower_id == follower.id, Message.status == MessageStatus.failed)
            .values(status=MessageStatus.pending, retry_count=0, error_message=None)
        )
        # If follower had a generated message → back to message_generated (skip AI step)
        # else → back to bio_scraped (AI must regenerate)
        follower.status = FollowerStatus.message_generated if follower.id in has_message else FollowerStatus.bio_scraped
        follower.locked_by_account_id = None
        follower.locked_at = None
        retry_count += 1

    campaign.messages_failed = max(0, campaign.messages_failed - retry_count)
    campaign.messages_pending = campaign.messages_pending + retry_count
    campaign.updated_at = datetime.utcnow()

    log = ActivityLog(
        campaign_id=campaign.id,
        action="retry_failed",
        details=json.dumps({"count": retry_count}),
    )
    db.add(log)
    await db.commit()
    await db.refresh(campaign)
    return await _enrich_campaign(campaign, db, include_today=True)


@router.get("/{campaign_id}/events")
async def get_campaign_events(
    campaign_id: str,
    since_id: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """Real-time worker event feed for the live log panel in the UI."""
    await _get_or_404(campaign_id, db)
    from app.utils.events import get_events
    events = get_events(campaign_id, since_id=since_id, limit=200)
    return {"events": events, "last_id": events[-1]["id"] if events else since_id}


async def _get_or_404(campaign_id: str, db: AsyncSession) -> Campaign:
    result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


async def _check_redis_reachable() -> bool:
    """Quick pre-flight check: is Redis up? Used before setting campaign to running."""
    return await check_redis_reachable()


def _arq_redis_settings():
    """ARQ RedisSettings with extended timeout for Memurai on Windows."""
    from app.services.work_enqueue import arq_redis_settings
    return arq_redis_settings()


async def _enqueue_scrape(campaign_id: str):
    """Enqueue the scrape task via ARQ."""
    try:
        from app.services.work_enqueue import enqueue_scrape
        await enqueue_scrape(campaign_id)
    except Exception as e:
        from loguru import logger
        logger.error(f"Failed to enqueue scrape task: {e}")


async def _enqueue_pregen(campaign_id: str):
    """Enqueue the pre-generation task via ARQ."""
    try:
        import arq
        from app.services.work_enqueue import ARQ_MAIN_QUEUE
        redis = await arq.create_pool(_arq_redis_settings())
        job_id = f"pregen:{campaign_id}:preview"
        await redis.delete(f"arq:job:{job_id}", f"arq:retry:{job_id}")
        await redis.enqueue_job(
            "pre_generate_messages_task",
            campaign_id,
            _job_id=job_id,
            _queue_name=ARQ_MAIN_QUEUE,
        )
        await redis.aclose()
    except Exception as e:
        from loguru import logger
        from app.utils.events import emit as emit_event
        emit_event(campaign_id, "pregen_error", f"Impossibile avviare pre-generazione: {str(e)[:120]}", level="error")
        logger.error(f"Failed to enqueue pre-gen task for campaign {campaign_id}: {e}")


async def _enqueue_full_batch(campaign_id: str):
    """Enqueue full batch generation after preview approval."""
    try:
        import arq
        from app.services.work_enqueue import ARQ_MAIN_QUEUE
        from app.utils.events import emit as emit_event
        redis = await arq.create_pool(_arq_redis_settings())
        job_id = f"pregen:{campaign_id}:full"
        await redis.delete(f"arq:job:{job_id}", f"arq:retry:{job_id}")
        await redis.enqueue_job(
            "full_batch_generate_task",
            campaign_id,
            _job_id=job_id,
            _queue_name=ARQ_MAIN_QUEUE,
        )
        await redis.aclose()
    except Exception as e:
        from loguru import logger
        from app.utils.events import emit as emit_event
        emit_event(campaign_id, "pregen_error", f"Impossibile avviare batch: {str(e)[:120]}", level="error")
        logger.error(f"Failed to enqueue full batch for campaign {campaign_id}: {e}")


async def _enqueue_campaign_run(campaign_id: str):
    """Enqueue one worker task per assigned active account.

    If 0 workers got enqueued (account removed/disabled between API pre-check
    and this background task), revert the campaign to 'paused' so it doesn't
    appear running while idle.
    """
    from loguru import logger
    try:
        from app.services.work_enqueue import enqueue_campaign_run
        count = await enqueue_campaign_run(campaign_id)
        logger.info(f"Enqueued {count} worker(s) for campaign {campaign_id}")
        if count == 0:
            from app.database import AsyncSessionLocal
            async with AsyncSessionLocal() as db:
                camp = await db.scalar(select(Campaign).where(Campaign.id == campaign_id))
                if camp and camp.status == CampaignStatus.scraping_and_running:
                    # Scraper still has work — drop to scraping-only instead of pausing
                    camp.status = CampaignStatus.scraping
                    camp.auto_generate = False
                    camp.updated_at = datetime.utcnow()
                    db.add(ActivityLog(
                        campaign_id=campaign_id,
                        action="campaign_dm_degraded",
                        details='{"reason":"zero_workers_enqueued"}',
                    ))
                    await db.commit()
                    logger.warning(
                        f"Campaign {campaign_id} dropped to scraping: 0 DM workers enqueued"
                    )
                elif camp and camp.status == CampaignStatus.running:
                    previous_status = camp.status.value
                    camp.status = CampaignStatus.paused
                    camp.updated_at = datetime.utcnow()
                    db.add(ActivityLog(
                        campaign_id=campaign_id,
                        action="campaign_auto_paused",
                        details=json.dumps(
                            {
                                "reason": "zero_workers_enqueued",
                                "previous_status": previous_status,
                            }
                        ),
                    ))
                    await db.commit()
                    from app.utils.events import emit as emit_event
                    from app.services.notifier import send_campaign_auto_pause_alert
                    emit_event(
                        campaign_id,
                        "campaign_auto_paused",
                        "Campagna messa in pausa: nessun worker DM accodato al restart.",
                        level="warn",
                    )
                    await send_campaign_auto_pause_alert(
                        campaign_name=camp.name,
                        campaign_id=campaign_id,
                        reason="zero_workers_enqueued",
                        level="warning",
                        details={"previous_status": previous_status},
                    )
                    logger.warning(
                        f"Campaign {campaign_id} auto-paused: 0 workers enqueued "
                        f"(account removed or role changed during start)"
                    )
    except Exception as e:
        logger.error(f"Failed to enqueue campaign workers for {campaign_id}: {e}")
