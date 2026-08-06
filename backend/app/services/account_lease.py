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


async def release_slot(account_id: str, owner_prefix: str, db: AsyncSession) -> int:
    """Rilascia il lease lasciato da uno SLOT di lavoro, qualunque invocazione lo tenga.

    Il lease appartiene allo slot (`worker:{campaign}:{account}`), non alla singola
    invocazione: `run_campaign_worker` ci aggiunge un uuid per distinguersi, e a fine
    batch lo prolunga per tutta la pausa di sessione senza rilasciarlo (cosi' nessun
    altro job prende l'account durante la pausa). Se pero' quel job viene ri-accodato
    prima della scadenza, la nuova invocazione ha un owner diverso e resta fuori dal
    suo stesso lease -> "already leased by another job".

    Chiamare SOLO dopo aver verificato che ARQ non ha quel job in esecuzione
    (`arq:in-progress:{job_id}` assente): altrimenti si toglie il lease a un worker vivo.
    Il prefisso e' vincolato allo slot, quindi il lease di un'altra campagna sullo
    stesso account non viene toccato.
    """
    now = datetime.utcnow()
    result = await db.execute(
        update(InstagramAccount)
        .where(
            InstagramAccount.id == account_id,
            InstagramAccount.lease_owner.like(f"{owner_prefix}%"),
        )
        .values(lease_owner=None, lease_expires_at=None, updated_at=now)
    )
    await db.commit()
    return result.rowcount or 0


async def release(account_id: str, owner: str, db: AsyncSession) -> None:
    now = datetime.utcnow()
    await db.execute(
        update(InstagramAccount)
        .where(InstagramAccount.id == account_id, InstagramAccount.lease_owner == owner)
        .values(lease_owner=None, lease_expires_at=None, updated_at=now)
    )
    await db.commit()
