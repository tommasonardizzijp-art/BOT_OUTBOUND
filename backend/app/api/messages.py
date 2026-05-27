from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from app.database import get_db
from app.models.account import InstagramAccount
from app.models.campaign import Campaign, CampaignStatus
from app.models.message import Message, MessageStatus
from app.models.follower import Follower, FollowerStatus
from app.schemas.message import MessageResponse, MessageListResponse, MessageStats

router = APIRouter(prefix="/messages", tags=["messages"])


@router.get("", response_model=MessageListResponse)
async def list_messages(
    campaign_id: str | None = None,
    account_id: str | None = None,
    status: MessageStatus | None = None,
    replied_only: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(
            Message,
            Campaign.name.label("campaign_name"),
            Follower.username.label("follower_username"),
            Follower.full_name.label("follower_full_name"),
            Follower.status.label("follower_status"),
            InstagramAccount.username.label("account_username"),
        )
        .join(Campaign, Campaign.id == Message.campaign_id)
        .join(Follower, Follower.id == Message.follower_id)
        .outerjoin(InstagramAccount, InstagramAccount.id == Message.account_id)
    )
    if campaign_id:
        query = query.where(Message.campaign_id == campaign_id)
    if account_id:
        query = query.where(Message.account_id == account_id)
    if status:
        query = query.where(Message.status == status)
    if replied_only:
        query = query.where(Follower.status == FollowerStatus.replied)

    total_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar_one()

    query = query.offset((page - 1) * page_size).limit(page_size).order_by(Message.created_at.desc())
    result = await db.execute(query)
    items = [
        MessageResponse.model_validate(row[0]).model_copy(update={
            "campaign_name": row.campaign_name,
            "follower_username": row.follower_username,
            "follower_full_name": row.follower_full_name,
            "account_username": row.account_username,
            "has_reply": row.follower_status == FollowerStatus.replied,
        })
        for row in result.all()
    ]

    return MessageListResponse(items=items, total=total, page=page, page_size=page_size)


@router.get("/stats", response_model=MessageStats)
async def get_message_stats(
    period: str | None = Query(default=None, description="24h|7d|30d|6m"),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    campaign_id: str | None = Query(default=None),
    account_id: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.utcnow()
    start: datetime | None = None
    end: datetime = now

    if date_from:
        try:
            start = datetime.fromisoformat(date_from)
        except ValueError:
            pass
    if date_to:
        try:
            end = datetime.fromisoformat(date_to)
        except ValueError:
            pass
    if not date_from and not date_to and period:
        days_map = {'24h': 1, '7d': 7, '30d': 30, '6m': 180}
        start = now - timedelta(days=days_map.get(period, 1))

    def base_conds(extra=None):
        c = [Message.created_at <= end]
        if start:
            c.append(Message.created_at >= start)
        if campaign_id:
            c.append(Message.campaign_id == campaign_id)
        if account_id:
            c.append(Message.account_id == account_id)
        if extra:
            c.extend(extra)
        return c

    total_sent = await db.scalar(
        select(func.count(Message.id)).where(and_(*base_conds([Message.status == MessageStatus.sent])))
    ) or 0
    total_failed = await db.scalar(
        select(func.count(Message.id)).where(and_(*base_conds([Message.status == MessageStatus.failed])))
    ) or 0

    # Count followers who replied among those we successfully DMed in the period
    sent_follower_sq = (
        select(Message.follower_id)
        .where(and_(*base_conds([Message.status == MessageStatus.sent])))
        .subquery()
    )
    total_replied = await db.scalar(
        select(func.count(Follower.id))
        .where(
            Follower.id.in_(select(sent_follower_sq.c.follower_id)),
            Follower.status == FollowerStatus.replied,
        )
    ) or 0

    total = total_sent + total_failed
    return MessageStats(
        total_sent=total_sent,
        total_failed=total_failed,
        total_replied=total_replied,
        success_rate=round(total_sent / total * 100, 1) if total > 0 else 0.0,
        reply_rate=round(total_replied / total_sent * 100, 1) if total_sent > 0 else 0.0,
    )


@router.post("/{message_id}/retry", response_model=MessageResponse)
async def retry_message(message_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Message).where(Message.id == message_id))
    message = result.scalar_one_or_none()
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")

    if message.status not in (MessageStatus.failed,):
        raise HTTPException(status_code=400, detail="Only failed messages can be retried")

    # BUG-NEW-06: no worker will pick up the retry unless the campaign is running
    camp_result = await db.execute(select(Campaign).where(Campaign.id == message.campaign_id))
    campaign = camp_result.scalar_one_or_none()
    if not campaign or campaign.status != CampaignStatus.running:
        raise HTTPException(
            status_code=400,
            detail="La campagna non è in esecuzione. Avvia la campagna prima di ritentare il messaggio.",
        )

    message.status = MessageStatus.retry
    message.retry_count = 0

    # Also unblock the follower so the orchestrator can pick it up again
    f_result = await db.execute(select(Follower).where(Follower.id == message.follower_id))
    follower = f_result.scalar_one_or_none()
    if follower and follower.status == FollowerStatus.failed:
        follower.status = FollowerStatus.message_generated

    await db.commit()
    await db.refresh(message)
    return message


@router.post("/recover-sending")
async def recover_sending_messages_endpoint(db: AsyncSession = Depends(get_db)):
    """
    Manually trigger recovery of Message rows stuck in status='sending'.
    These rows arise when send_dm() crashes after pressing Enter but before the
    'sent' DB commit. The recovery checker uses instagrapi to confirm delivery.
    Returns counts of recovered, retried, skipped, and errored messages.
    """
    from app.services.recovery_checker import recover_sending_messages
    counts = await recover_sending_messages()
    return {
        "triggered": True,
        "recovered": counts["recovered"],
        "retried": counts["retried"],
        "skipped": counts["skipped"],
        "errors": counts["errors"],
    }
