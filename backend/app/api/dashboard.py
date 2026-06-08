from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models.account import InstagramAccount, AccountStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.message import Message, MessageStatus
from app.models.activity_log import ActivityLog
from app.schemas.dashboard import DashboardStats, ActivityLogListResponse, ActivityLogResponse, TimelineResponse, HourlyPoint

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/stats", response_model=DashboardStats)
async def get_stats(db: AsyncSession = Depends(get_db)):
    # Accounts
    total_accounts = await _count(db, InstagramAccount)
    active_accounts = await _count(db, InstagramAccount, InstagramAccount.status == AccountStatus.active)
    cooldown_accounts = await _count(db, InstagramAccount, InstagramAccount.status == AccountStatus.cooldown)
    banned_accounts = await _count(db, InstagramAccount, InstagramAccount.status == AccountStatus.banned)

    # Campaigns
    total_campaigns = await _count(db, Campaign)
    running_campaigns = await _count(
        db, Campaign, Campaign.status.in_([
            CampaignStatus.running,
            CampaignStatus.listing,
            CampaignStatus.listing_break,
            CampaignStatus.scraping,
            CampaignStatus.scraping_and_running,
            CampaignStatus.scraping_break,
        ])
    )

    # Messages
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    sent_today = await _count(db, Message, Message.status == MessageStatus.sent, Message.sent_at >= today_start)
    sent_total = await _count(db, Message, Message.status == MessageStatus.sent)
    failed_total = await _count(db, Message, Message.status == MessageStatus.failed)

    success_rate = (sent_total / (sent_total + failed_total) * 100) if (sent_total + failed_total) > 0 else 0.0

    return DashboardStats(
        total_accounts=total_accounts,
        active_accounts=active_accounts,
        accounts_in_cooldown=cooldown_accounts,
        accounts_banned=banned_accounts,
        total_campaigns=total_campaigns,
        running_campaigns=running_campaigns,
        messages_sent_today=sent_today,
        messages_sent_total=sent_total,
        messages_failed_total=failed_total,
        success_rate=round(success_rate, 1),
    )


@router.get("/activity", response_model=ActivityLogListResponse)
async def get_activity(
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ActivityLog).order_by(ActivityLog.created_at.desc()).limit(limit)
    )
    items = result.scalars().all()
    total = await _count(db, ActivityLog)
    return ActivityLogListResponse(
        items=[ActivityLogResponse.model_validate(i) for i in items],
        total=total,
    )


@router.get("/timeline", response_model=TimelineResponse)
async def get_timeline(
    period: str = Query(default="24h", pattern="^(24h|7d|30d|6m)$"),
    db: AsyncSession = Depends(get_db),
):
    """Return message timeline. Periods: 24h (by hour), 7d (by day), 30d (by day), 6m (by week)."""
    now = datetime.utcnow()
    if period == "24h":
        since = now - timedelta(hours=24)
        group_fmt = "%Y-%m-%dT%H:00"   # "2026-04-17T14:00"
    elif period == "7d":
        since = now - timedelta(days=7)
        group_fmt = "%Y-%m-%d"          # "2026-04-17"
    elif period == "30d":
        since = now - timedelta(days=30)
        group_fmt = "%Y-%m-%d"
    else:  # 6m
        since = now - timedelta(days=183)
        # Group by week start (Monday) — truncate to nearest Monday
        group_fmt = None  # handled separately

    result = await db.execute(
        select(Message.sent_at).where(
            Message.status == MessageStatus.sent,
            Message.sent_at >= since,
        )
    )
    timestamps = [row[0] for row in result.fetchall() if row[0]]

    counts: dict[str, int] = {}
    for ts in timestamps:
        if group_fmt:
            key = ts.strftime(group_fmt)
        else:
            # Weekly: round down to Monday of that week
            days_since_monday = ts.weekday()  # 0=Mon
            week_start = (ts - timedelta(days=days_since_monday)).date()
            key = str(week_start)
        counts[key] = counts.get(key, 0) + 1

    # For 24h: fill in missing hours with 0 so chart has a continuous grid
    if period == "24h":
        for i in range(24):
            h = (now - timedelta(hours=23 - i)).strftime("%Y-%m-%dT%H:00")
            counts.setdefault(h, 0)

    data = [HourlyPoint(hour=h, count=c) for h, c in sorted(counts.items())]
    return TimelineResponse(data=data)


async def _count(db: AsyncSession, model, *filters) -> int:
    q = select(func.count()).select_from(model)
    for f in filters:
        q = q.where(f)
    result = await db.execute(q)
    return result.scalar_one()
