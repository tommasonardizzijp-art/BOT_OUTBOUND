from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case, delete
from app.database import get_db
from app.models.follower import Follower, FollowerStatus
from app.models.message import Message, MessageStatus
from app.models.campaign import Campaign
from app.schemas.follower import FollowerResponse, FollowerListResponse

router = APIRouter(prefix="/campaigns/{campaign_id}/followers", tags=["followers"])

# Priority order for contact_order sort: 0=sent first, higher=later
_STATUS_PRIORITY = case(
    (Follower.status == FollowerStatus.sent, 0),
    (Follower.status == FollowerStatus.replied, 1),
    (Follower.status == FollowerStatus.message_generated, 2),
    (Follower.status == FollowerStatus.pending_approval, 2),
    (Follower.status == FollowerStatus.bio_scraped, 3),
    (Follower.status == FollowerStatus.pending, 4),
    (Follower.status == FollowerStatus.failed, 5),
    (Follower.status == FollowerStatus.skipped, 6),
    else_=7,
)


@router.get("", response_model=FollowerListResponse)
async def list_followers(
    campaign_id: str,
    status: FollowerStatus | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    sort_by: str = Query(default="updated_at_desc", pattern="^(updated_at_desc|contact_order)$"),
    db: AsyncSession = Depends(get_db),
):
    await _campaign_or_404(campaign_id, db)

    query = select(Follower).where(Follower.campaign_id == campaign_id)
    if status:
        query = query.where(Follower.status == status)

    count_where = [Follower.campaign_id == campaign_id]
    if status:
        count_where.append(Follower.status == status)
    total_result = await db.execute(select(func.count(Follower.id)).where(*count_where))
    total = total_result.scalar_one()

    if sort_by == "contact_order":
        query = query.offset((page - 1) * page_size).limit(page_size).order_by(
            _STATUS_PRIORITY, Follower.updated_at.asc()
        )
    else:
        query = query.offset((page - 1) * page_size).limit(page_size).order_by(Follower.updated_at.desc())
    result = await db.execute(query)
    items = result.scalars().all()

    # Attach pre-generated message text to each follower
    if items:
        follower_ids = [f.id for f in items]
        msg_result = await db.execute(
            select(Message.follower_id, Message.generated_text, Message.template_variant)
            .where(Message.follower_id.in_(follower_ids))
        )
        msg_map = {row[0]: (row[1], row[2]) for row in msg_result.fetchall()}

        enriched = []
        for f in items:
            data = FollowerResponse.model_validate(f)
            if f.id in msg_map:
                data.generated_text = msg_map[f.id][0]
                data.template_variant = msg_map[f.id][1]
            enriched.append(data)
        return FollowerListResponse(items=enriched, total=total, page=page, page_size=page_size)

    return FollowerListResponse(items=items, total=total, page=page, page_size=page_size)


@router.post("/{follower_id}/skip", response_model=FollowerResponse)
async def skip_follower(campaign_id: str, follower_id: str, db: AsyncSession = Depends(get_db)):
    follower = await _follower_or_404(follower_id, campaign_id, db)

    if follower.status == FollowerStatus.sent:
        raise HTTPException(status_code=400, detail="Cannot skip an already-sent follower")

    follower.status = FollowerStatus.skipped
    follower.skip_reason = "manually_skipped"
    await db.commit()
    await db.refresh(follower)
    return follower


@router.post("/{follower_id}/regenerate", response_model=FollowerResponse)
async def regenerate_message(campaign_id: str, follower_id: str, db: AsyncSession = Depends(get_db)):
    """Regenerate AI message inline using the current campaign template."""
    from app.services.ai_personalizer import compose_message

    follower = await _follower_or_404(follower_id, campaign_id, db)

    if follower.status == FollowerStatus.sent:
        raise HTTPException(status_code=400, detail="Cannot regenerate message for an already-sent follower")

    if follower.status not in (
        FollowerStatus.bio_scraped, FollowerStatus.message_generated,
        FollowerStatus.failed, FollowerStatus.pending_approval,
    ):
        raise HTTPException(status_code=400, detail="Follower must have bio scraped before regenerating")

    campaign_result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = campaign_result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Delete existing messages so no orphan duplicates remain
    await db.execute(delete(Message).where(Message.follower_id == follower_id))

    try:
        text, variant = await compose_message(campaign, follower)
    except Exception as e:
        follower.status = FollowerStatus.bio_scraped
        await db.commit()
        raise HTTPException(status_code=500, detail=f"Generazione fallita: {str(e)[:120]}")

    new_msg = Message(
        campaign_id=campaign_id,
        follower_id=follower.id,
        generated_text=text,
        status=MessageStatus.pending,
        template_variant=variant,
    )
    db.add(new_msg)
    follower.status = FollowerStatus.pending_approval if campaign.require_approval else FollowerStatus.message_generated
    await db.commit()
    await db.refresh(follower)
    return follower


@router.post("/{follower_id}/requeue", response_model=FollowerResponse)
async def requeue_follower(campaign_id: str, follower_id: str, db: AsyncSession = Depends(get_db)):
    """Re-queue a failed or skipped follower for another attempt.
    Resets status to bio_scraped (AI will regenerate message).
    Clears locks and resets associated message if exists."""
    follower = await _follower_or_404(follower_id, campaign_id, db)

    if follower.status not in (FollowerStatus.failed, FollowerStatus.skipped):
        raise HTTPException(status_code=400, detail="Solo follower falliti o saltati possono essere rimessi in coda")

    old_status = follower.status

    # Reset associated failed message
    msg_result = await db.execute(
        select(Message).where(Message.follower_id == follower_id)
    )
    msg = msg_result.scalar_one_or_none()
    if msg:
        await db.delete(msg)

    # Reset follower to bio_scraped so AI re-generates
    follower.status = FollowerStatus.bio_scraped
    follower.skip_reason = None
    follower.locked_by_account_id = None
    follower.locked_at = None

    # Update campaign counters
    campaign = await _campaign_or_404(campaign_id, db)
    if old_status == FollowerStatus.failed:
        campaign.messages_failed = max(0, campaign.messages_failed - 1)
    campaign.messages_pending = campaign.messages_pending + 1
    campaign.updated_at = func.now()

    await db.commit()
    await db.refresh(follower)
    return follower


async def _campaign_or_404(campaign_id: str, db: AsyncSession) -> Campaign:
    result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


async def _follower_or_404(follower_id: str, campaign_id: str, db: AsyncSession) -> Follower:
    result = await db.execute(
        select(Follower).where(Follower.id == follower_id, Follower.campaign_id == campaign_id)
    )
    follower = result.scalar_one_or_none()
    if not follower:
        raise HTTPException(status_code=404, detail="Follower not found")
    return follower
