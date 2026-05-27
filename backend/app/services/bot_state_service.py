"""Bot global kill-switch service.

Single-row `bot_state` table holds a `halted` flag. When True, ALL workers
(DM + scraper) abort immediately. Only an admin (UI or Telegram /unhalt)
can clear the flag.

Used by:
- _claim_next_follower (orchestrator) — refuses to claim if halted
- scraper main loop — exits gracefully on next iteration
- anomaly_detector — sets the flag on critical anomalies
- /admin/halt /admin/resume API endpoints
- Telegram /halt /unhalt commands
"""
from datetime import datetime
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.database import AsyncSessionLocal
from app.models.bot_state import BotState


async def _ensure_row(db: AsyncSession) -> BotState:
    row = await db.scalar(select(BotState).where(BotState.id == 1))
    if row is None:
        row = BotState(id=1, halted=False)
        db.add(row)
        await db.flush()
    return row


async def is_halted(db: AsyncSession | None = None) -> bool:
    """Cheap check used inside hot loops. Opens a short session if none provided."""
    if db is not None:
        row = await db.scalar(select(BotState.halted).where(BotState.id == 1))
        return bool(row)
    async with AsyncSessionLocal() as s:
        row = await s.scalar(select(BotState.halted).where(BotState.id == 1))
        return bool(row)


async def get_state(db: AsyncSession | None = None) -> BotState:
    if db is not None:
        return await _ensure_row(db)
    async with AsyncSessionLocal() as s:
        row = await _ensure_row(s)
        await s.commit()
        return row


async def halt(
    *,
    reason: str,
    kind: str | None = None,
    by: str = "system",
    db: AsyncSession | None = None,
) -> None:
    """Set the kill-switch. Idempotent — refreshes reason if already halted."""
    own_session = db is None
    if own_session:
        db = AsyncSessionLocal()
    try:
        await db.execute(
            update(BotState).where(BotState.id == 1).values(
                halted=True,
                halted_reason=reason[:1000],
                halted_kind=(kind or None),
                halted_at=datetime.utcnow(),
                halted_by=by[:255],
            )
        )
        # Insert if missing (rowcount check). Rare race.
        existing = await db.scalar(select(BotState).where(BotState.id == 1))
        if existing is None:
            db.add(BotState(
                id=1, halted=True,
                halted_reason=reason[:1000], halted_kind=kind,
                halted_at=datetime.utcnow(), halted_by=by[:255],
            ))
        if own_session:
            await db.commit()
        logger.warning(f"[BotState] HALTED by={by} kind={kind} reason={reason[:200]}")
    except Exception as e:
        logger.error(f"[BotState] halt() failed: {e}")
        if own_session:
            try:
                await db.rollback()
            except Exception:
                pass
        raise
    finally:
        if own_session:
            await db.close()


async def resume(*, by: str = "user", db: AsyncSession | None = None) -> bool:
    """Clear the kill-switch. Returns True if state was halted, False if no-op."""
    own_session = db is None
    if own_session:
        db = AsyncSessionLocal()
    try:
        row = await db.scalar(select(BotState).where(BotState.id == 1))
        if row is None or not row.halted:
            return False
        await db.execute(
            update(BotState).where(BotState.id == 1).values(
                halted=False,
                halted_reason=None,
                halted_kind=None,
                halted_at=None,
                halted_by=None,
                last_resume_at=datetime.utcnow(),
                last_resume_by=by[:255],
            )
        )
        if own_session:
            await db.commit()
        logger.info(f"[BotState] RESUMED by={by}")
        return True
    except Exception as e:
        logger.error(f"[BotState] resume() failed: {e}")
        if own_session:
            try:
                await db.rollback()
            except Exception:
                pass
        raise
    finally:
        if own_session:
            await db.close()
