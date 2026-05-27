from datetime import datetime

import pytest

from app.database import AsyncSessionLocal
from app.services import reservation


@pytest.mark.asyncio
async def test_reserve_is_exclusive_then_releasable():
    ig_user_id = 880000000000 + int(datetime.utcnow().timestamp()) % 100000
    async with AsyncSessionLocal() as db:
        assert await reservation.try_reserve(ig_user_id, "jobA", "c1", db) is True
        assert await reservation.try_reserve(ig_user_id, "jobB", "c1", db) is False
        await reservation.release(ig_user_id, db)
        await db.commit()
        assert await reservation.try_reserve(ig_user_id, "jobB", "c1", db) is True
        await reservation.release(ig_user_id, db)
        await db.commit()
