"""Mini-sessione browser: rispetta il cap, scrapa i claimati, ritorna il defer."""
from datetime import datetime
import pytest

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.services import browser_bio


class _FakeSession:
    def __init__(self, *a, **k): self.opened = False; self.closed = False
    async def open(self): self.opened = True
    async def close(self): self.closed = True
    class _P:
        async def ensure_logged_in(self, account_id): return None
    page = _P()


@pytest.mark.asyncio
async def test_session_scrapes_up_to_cap_and_returns_defer(monkeypatch):
    base = 960000000000 + int(datetime.utcnow().timestamp()) % 100000
    async with AsyncSessionLocal() as db:
        camp = Campaign(name="t", status=CampaignStatus.scraping, source_type="scrape")
        db.add(camp); await db.flush()
        for i in range(5):
            db.add(Follower(campaign_id=camp.id, ig_user_id=base + i,
                            username=f"u{base+i}", status=FollowerStatus.pending))
        await db.commit()
        cid = camp.id

    # cap piccolo per forzare il defer prima di esaurire i pending
    monkeypatch.setattr(browser_bio, "BrowserSession", _FakeSession)
    monkeypatch.setattr(browser_bio, "pick_session_cap", lambda *a, **k: 2)

    async def fake_fetch(follower, campaign, db, session):
        follower.status = FollowerStatus.bio_scraped
        follower.locked_by_account_id = None
        follower.locked_at = None
        await db.commit()
        return "done", None
    monkeypatch.setattr(browser_bio, "fetch_and_store_bio_browser", fake_fetch)
    monkeypatch.setattr(browser_bio, "human_profile_pause", lambda: _anoop())
    monkeypatch.setattr(browser_bio, "maybe_micro_scroll", lambda *a, **k: _anoop_false())

    defer = await browser_bio.scrape_bios_browser_session(cid, "acc-A")
    assert isinstance(defer, int) and defer >= 60      # pausa lunga → defer
    async with AsyncSessionLocal() as db:
        from sqlalchemy import select, func
        done = await db.scalar(
            select(func.count()).select_from(Follower).where(
                Follower.campaign_id == cid,
                Follower.status == FollowerStatus.bio_scraped))
        assert done == 2                                # esattamente il cap


async def _anoop(): return None
async def _anoop_false(): return False
