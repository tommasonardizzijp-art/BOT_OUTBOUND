"""Temporary contact reservations.

global_contacts means a contact was worked. contact_reservations is only a
short-lived lease that prevents concurrent workers from targeting the same IG
user while a DM attempt is in flight.
"""
from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.contact_reservation import ContactReservation
from app.utils.db_dialect import upsert_ignore


RESERVATION_TTL_MINUTES = 30


async def try_reserve(ig_user_id: int, owner_job: str, campaign_id: str, db: AsyncSession) -> bool:
    now = datetime.utcnow()
    await db.execute(delete(ContactReservation).where(ContactReservation.expires_at < now))
    stmt = upsert_ignore(
        ContactReservation,
        {
            "ig_user_id": ig_user_id,
            "owner_job": owner_job,
            "campaign_id": campaign_id,
            "created_at": now,
            "expires_at": now + timedelta(minutes=RESERVATION_TTL_MINUTES),
        },
        "ig_user_id",
        settings.database_url,
    )
    result = await db.execute(stmt)
    await db.commit()
    return result.rowcount == 1


async def release(ig_user_id: int, db: AsyncSession) -> None:
    await db.execute(delete(ContactReservation).where(ContactReservation.ig_user_id == ig_user_id))
