"""Cooperative DB lease for one worker per Instagram account."""
from datetime import datetime, timedelta

from sqlalchemy import or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import InstagramAccount


async def acquire(account_id: str, owner: str, db: AsyncSession, ttl_min: int = 15) -> bool:
    now = datetime.utcnow()
    result = await db.execute(
        update(InstagramAccount)
        .where(
            InstagramAccount.id == account_id,
            or_(
                InstagramAccount.lease_owner.is_(None),
                InstagramAccount.lease_owner == owner,
                InstagramAccount.lease_expires_at < now,
            ),
        )
        .values(
            lease_owner=owner,
            lease_expires_at=now + timedelta(minutes=ttl_min),
            updated_at=now,
        )
    )
    await db.commit()
    return result.rowcount == 1


async def heartbeat(account_id: str, owner: str, db: AsyncSession, ttl_min: int = 15) -> bool:
    now = datetime.utcnow()
    result = await db.execute(
        update(InstagramAccount)
        .where(InstagramAccount.id == account_id, InstagramAccount.lease_owner == owner)
        .values(lease_expires_at=now + timedelta(minutes=ttl_min), updated_at=now)
    )
    await db.commit()
    return result.rowcount == 1


async def hold_for_seconds(account_id: str, owner: str, db: AsyncSession, seconds: int) -> bool:
    now = datetime.utcnow()
    result = await db.execute(
        update(InstagramAccount)
        .where(InstagramAccount.id == account_id, InstagramAccount.lease_owner == owner)
        .values(lease_expires_at=now + timedelta(seconds=seconds), updated_at=now)
    )
    await db.commit()
    return result.rowcount == 1


async def release(account_id: str, owner: str, db: AsyncSession) -> None:
    now = datetime.utcnow()
    await db.execute(
        update(InstagramAccount)
        .where(InstagramAccount.id == account_id, InstagramAccount.lease_owner == owner)
        .values(lease_owner=None, lease_expires_at=None, updated_at=now)
    )
    await db.commit()
