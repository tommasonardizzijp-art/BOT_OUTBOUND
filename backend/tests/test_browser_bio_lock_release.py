"""C2: dopo scrape bio via browser, il lock del claim va rilasciato."""
from datetime import datetime
import pytest

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.services import browser_bio


class _FakePage:
    def __init__(self, user): self._user = user
    async def _get_page(self): return self  # non usato: patchiamo _capture_web_profile_info


class _FakeSession:
    def __init__(self, user): self.page = _FakePage(user)


@pytest.mark.asyncio
async def test_lock_released_on_done(monkeypatch):
    uid = 990000000000 + int(datetime.utcnow().timestamp()) % 100000
    async with AsyncSessionLocal() as db:
        camp = Campaign(name="t", status=CampaignStatus.scraping, source_type="scrape")
        db.add(camp); await db.flush()
        f = Follower(
            campaign_id=camp.id, ig_user_id=uid, username=f"u{uid}",
            status=FollowerStatus.pending,
            locked_by_account_id="acc-1", locked_at=datetime.utcnow(),
        )
        db.add(f); await db.commit(); await db.refresh(f)

        async def fake_capture(raw_page, username, timeout_s=8.0):
            return {"id": str(uid), "username": username, "full_name": "X",
                    "biography": "bio", "edge_followed_by": {"count": 1},
                    "edge_follow": {"count": 1}}
        monkeypatch.setattr(browser_bio, "_capture_web_profile_info", fake_capture)

        outcome, err = await browser_bio.fetch_and_store_bio_browser(f, camp, db, _FakeSession({}))
        assert outcome == "done"
        await db.refresh(f)
        assert f.status == FollowerStatus.bio_scraped
        assert f.locked_by_account_id is None
        assert f.locked_at is None
