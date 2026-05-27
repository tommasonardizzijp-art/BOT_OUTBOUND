"""Operational diagnostics for administrators."""
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.account import InstagramAccount
from app.models.campaign import Campaign, CampaignStatus
from app.models.contact_reservation import ContactReservation
from app.models.follower import Follower
from app.models.message import Message, MessageStatus
from app.models.user import User
from app.utils.auth_deps import require_admin


router = APIRouter(prefix="/ops", tags=["ops"])


@router.get("/summary")
async def ops_summary(
    _: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
) -> dict:
    now = datetime.utcnow()
    sending_cutoff = now - timedelta(minutes=10)
    lock_cutoff = now - timedelta(minutes=20)
    campaign_cutoff = now - timedelta(minutes=30)

    sending_rows = (
        await db.execute(
            select(Message)
            .where(Message.status == MessageStatus.sending, Message.updated_at < sending_cutoff)
            .order_by(Message.updated_at.asc())
            .limit(25)
        )
    ).scalars().all()

    expired_reservations = (
        await db.execute(
            select(ContactReservation)
            .where(ContactReservation.expires_at < now)
            .order_by(ContactReservation.expires_at.asc())
            .limit(25)
        )
    ).scalars().all()

    stale_locks = (
        await db.execute(
            select(Follower)
            .where(Follower.locked_by_account_id.isnot(None), Follower.locked_at < lock_cutoff)
            .order_by(Follower.locked_at.asc())
            .limit(25)
        )
    ).scalars().all()

    stale_campaigns = (
        await db.execute(
            select(Campaign)
            .where(
                Campaign.status.in_([CampaignStatus.running, CampaignStatus.scraping_and_running]),
                Campaign.updated_at < campaign_cutoff,
            )
            .order_by(Campaign.updated_at.asc())
            .limit(25)
        )
    ).scalars().all()

    account_status_rows = (
        await db.execute(
            select(InstagramAccount.status, func.count(InstagramAccount.id)).group_by(InstagramAccount.status)
        )
    ).all()

    return {
        "generated_at": now.isoformat(),
        "sending_stale": {
            "count": len(sending_rows),
            "items": [
                {
                    "id": m.id,
                    "campaign_id": m.campaign_id,
                    "follower_id": m.follower_id,
                    "account_id": m.account_id,
                    "updated_at": m.updated_at.isoformat() if m.updated_at else None,
                }
                for m in sending_rows
            ],
        },
        "expired_reservations": {
            "count": len(expired_reservations),
            "items": [
                {
                    "ig_user_id": r.ig_user_id,
                    "owner_job": r.owner_job,
                    "campaign_id": r.campaign_id,
                    "expires_at": r.expires_at.isoformat(),
                }
                for r in expired_reservations
            ],
        },
        "stale_follower_locks": {
            "count": len(stale_locks),
            "items": [
                {
                    "id": f.id,
                    "username": f.username,
                    "campaign_id": f.campaign_id,
                    "locked_by_account_id": f.locked_by_account_id,
                    "locked_at": f.locked_at.isoformat() if f.locked_at else None,
                }
                for f in stale_locks
            ],
        },
        "stale_campaigns": {
            "count": len(stale_campaigns),
            "items": [
                {
                    "id": c.id,
                    "name": c.name,
                    "status": c.status.value,
                    "updated_at": c.updated_at.isoformat(),
                }
                for c in stale_campaigns
            ],
        },
        "accounts_by_status": {str(status.value): count for status, count in account_status_rows},
    }
