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


@pytest.mark.asyncio
async def test_skip_outcome_releases_lock_and_continues(monkeypatch):
    base = 961000000000 + int(datetime.utcnow().timestamp()) % 100000
    async with AsyncSessionLocal() as db:
        camp = Campaign(name="t", status=CampaignStatus.scraping, source_type="scrape")
        db.add(camp); await db.flush()
        for i in range(5):
            db.add(Follower(campaign_id=camp.id, ig_user_id=base + i,
                            username=f"u{base+i}", status=FollowerStatus.pending))
        await db.commit()
        cid = camp.id

    monkeypatch.setattr(browser_bio, "BrowserSession", _FakeSession)
    monkeypatch.setattr(browser_bio, "pick_session_cap", lambda *a, **k: 1)

    calls = {"n": 0}

    async def fake_fetch(follower, campaign, db, session):
        calls["n"] += 1
        if calls["n"] == 1:
            return "not_found", None
        follower.status = FollowerStatus.bio_scraped
        follower.locked_by_account_id = None
        follower.locked_at = None
        await db.commit()
        return "done", None
    monkeypatch.setattr(browser_bio, "fetch_and_store_bio_browser", fake_fetch)
    monkeypatch.setattr(browser_bio, "human_profile_pause", lambda: _anoop())
    monkeypatch.setattr(browser_bio, "maybe_micro_scroll", lambda *a, **k: _anoop_false())

    defer = await browser_bio.scrape_bios_browser_session(cid, "acc-A")
    assert isinstance(defer, int) and defer >= 60  # cap (1) raggiunto -> pausa lunga

    async with AsyncSessionLocal() as db:
        from sqlalchemy import select
        skipped = (await db.execute(
            select(Follower).where(
                Follower.campaign_id == cid,
                Follower.status == FollowerStatus.skipped,
            )
        )).scalars().all()
        assert len(skipped) == 1
        assert skipped[0].skip_reason == "browser_not_found"
        assert skipped[0].locked_by_account_id is None

        done = (await db.execute(
            select(Follower).where(
                Follower.campaign_id == cid,
                Follower.status == FollowerStatus.bio_scraped,
            )
        )).scalars().all()
        assert len(done) == 1


@pytest.mark.asyncio
async def test_soft_block_stops_and_releases_lock(monkeypatch):
    base = 962000000000 + int(datetime.utcnow().timestamp()) % 100000
    async with AsyncSessionLocal() as db:
        camp = Campaign(name="t", status=CampaignStatus.scraping, source_type="scrape")
        db.add(camp); await db.flush()
        db.add(Follower(campaign_id=camp.id, ig_user_id=base,
                        username=f"u{base}", status=FollowerStatus.pending))
        await db.commit()
        cid = camp.id

    monkeypatch.setattr(browser_bio, "BrowserSession", _FakeSession)
    monkeypatch.setattr(browser_bio, "pick_session_cap", lambda *a, **k: 5)

    async def fake_fetch(follower, campaign, db, session):
        return "soft_block", Exception("429")
    monkeypatch.setattr(browser_bio, "fetch_and_store_bio_browser", fake_fetch)
    monkeypatch.setattr(browser_bio, "human_profile_pause", lambda: _anoop())
    monkeypatch.setattr(browser_bio, "maybe_micro_scroll", lambda *a, **k: _anoop_false())

    defer = await browser_bio.scrape_bios_browser_session(cid, "acc-A")
    assert defer is None  # sessione fermata, nessun defer di pausa lunga

    async with AsyncSessionLocal() as db:
        from sqlalchemy import select
        f = (await db.execute(
            select(Follower).where(Follower.campaign_id == cid)
        )).scalar_one()
        assert f.status == FollowerStatus.pending  # non bruciato
        assert f.locked_by_account_id is None       # lock rilasciato


@pytest.mark.asyncio
async def test_pool_exhausted_returns_none(monkeypatch):
    async with AsyncSessionLocal() as db:
        camp = Campaign(name="t", status=CampaignStatus.scraping, source_type="scrape")
        db.add(camp); await db.commit()
        cid = camp.id

    monkeypatch.setattr(browser_bio, "BrowserSession", _FakeSession)
    monkeypatch.setattr(browser_bio, "pick_session_cap", lambda *a, **k: 5)

    async def fake_fetch(follower, campaign, db, session):
        raise AssertionError("non deve essere chiamato: nessun pending")
    monkeypatch.setattr(browser_bio, "fetch_and_store_bio_browser", fake_fetch)
    monkeypatch.setattr(browser_bio, "human_profile_pause", lambda: _anoop())
    monkeypatch.setattr(browser_bio, "maybe_micro_scroll", lambda *a, **k: _anoop_false())

    defer = await browser_bio.scrape_bios_browser_session(cid, "acc-A")
    assert defer is None


@pytest.mark.asyncio
async def test_skip_heavy_pool_hits_backstop_short_defer(monkeypatch):
    base = 963000000000 + int(datetime.utcnow().timestamp()) % 100000
    cap = 2
    max_iterations = cap * browser_bio.MAX_SESSION_ITERATIONS_MULTIPLIER
    # pool abbondante: molto piu' grande del backstop, cosi' non si esaurisce prima
    n_followers = max_iterations + 10
    async with AsyncSessionLocal() as db:
        camp = Campaign(name="t", status=CampaignStatus.scraping, source_type="scrape")
        db.add(camp); await db.flush()
        for i in range(n_followers):
            db.add(Follower(campaign_id=camp.id, ig_user_id=base + i,
                            username=f"u{base+i}", status=FollowerStatus.pending))
        await db.commit()
        cid = camp.id

    monkeypatch.setattr(browser_bio, "BrowserSession", _FakeSession)
    monkeypatch.setattr(browser_bio, "pick_session_cap", lambda *a, **k: cap)

    calls = {"n": 0}

    async def fake_fetch(follower, campaign, db, session):
        calls["n"] += 1
        return "private", None  # mai 'done': il pool e' tutto skip-heavy
    monkeypatch.setattr(browser_bio, "fetch_and_store_bio_browser", fake_fetch)
    monkeypatch.setattr(browser_bio, "human_profile_pause", lambda: _anoop())
    monkeypatch.setattr(browser_bio, "maybe_micro_scroll", lambda *a, **k: _anoop_false())

    defer = await browser_bio.scrape_bios_browser_session(cid, "acc-A")
    assert defer == 60  # backstop -> defer breve, non la pausa lunga (30-45min)
    assert calls["n"] <= max_iterations
