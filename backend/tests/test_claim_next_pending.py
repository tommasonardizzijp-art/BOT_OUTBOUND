"""Claim atomico dei pending: pool disgiunti tra account + stale release."""
from datetime import datetime, timedelta
import pytest

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.services.browser_bio import claim_next_pending


async def _mk_campaign_with_pending(db, n):
    camp = Campaign(name="t", status=CampaignStatus.scraping, source_type="scrape")
    db.add(camp); await db.flush()
    base = 970000000000 + int(datetime.utcnow().timestamp()) % 100000
    for i in range(n):
        db.add(Follower(campaign_id=camp.id, ig_user_id=base + i,
                        username=f"u{base+i}", status=FollowerStatus.pending))
    await db.commit()
    return camp


@pytest.mark.asyncio
async def test_two_accounts_get_disjoint_followers():
    async with AsyncSessionLocal() as db:
        camp = await _mk_campaign_with_pending(db, 2)
        a = await claim_next_pending(db, camp.id, "acc-A")
        b = await claim_next_pending(db, camp.id, "acc-B")
        assert a is not None and b is not None
        assert a.id != b.id                      # pool disgiunti
        assert a.locked_by_account_id == "acc-A"
        assert b.locked_by_account_id == "acc-B"
        # esauriti: terzo claim None
        assert await claim_next_pending(db, camp.id, "acc-A") is None


@pytest.mark.asyncio
async def test_stale_lock_is_reclaimed():
    async with AsyncSessionLocal() as db:
        camp = await _mk_campaign_with_pending(db, 1)
        f = (await claim_next_pending(db, camp.id, "acc-dead"))
        assert f is not None
        # simula sessione morta: lock vecchio > 20 min, ancora pending
        f.locked_at = datetime.utcnow() - timedelta(minutes=25)
        await db.commit()
        again = await claim_next_pending(db, camp.id, "acc-live")
        assert again is not None and again.id == f.id
        assert again.locked_by_account_id == "acc-live"
