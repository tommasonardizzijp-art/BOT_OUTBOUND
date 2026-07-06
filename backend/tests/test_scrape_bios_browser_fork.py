"""Biforcazione: bio_engine=browser → fan-out, niente loop API."""
from datetime import datetime
import pytest
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus
from app.services import scrape_bios as sb


@pytest.mark.asyncio
async def test_browser_engine_calls_enqueue_and_skips_api(monkeypatch):
    async with AsyncSessionLocal() as db:
        camp = Campaign(name="t", status=CampaignStatus.scraping,
                        source_type="scrape", bio_engine="browser")
        db.add(camp); await db.commit()
        cid = camp.id

    called = {"enqueue": 0, "pool": 0}

    async def fake_enqueue(campaign_id):
        called["enqueue"] += 1
        assert campaign_id == cid
        return 1
    # se entrasse nel path API costruirebbe la ScrapingPool → lo intercettiamo
    async def fake_build(*a, **k):
        called["pool"] += 1
        raise AssertionError("non deve entrare nel path API")

    monkeypatch.setattr("app.services.browser_bio.enqueue_browser_bio_workers", fake_enqueue)
    monkeypatch.setattr(sb.ScrapingPool, "build", staticmethod(fake_build))

    result = await sb.scrape_bios(cid)
    assert result is None
    assert called["enqueue"] == 1
    assert called["pool"] == 0


@pytest.mark.asyncio
async def test_browser_engine_no_account_sets_error(monkeypatch):
    async with AsyncSessionLocal() as db:
        camp = Campaign(name="t", status=CampaignStatus.scraping,
                        source_type="scrape", bio_engine="browser")
        db.add(camp); await db.commit()
        cid = camp.id

    async def fake_enqueue_zero(campaign_id):
        return 0  # nessun account scraping disponibile per il motore browser

    monkeypatch.setattr("app.services.browser_bio.enqueue_browser_bio_workers", fake_enqueue_zero)

    result = await sb.scrape_bios(cid)
    assert result is None

    async with AsyncSessionLocal() as db:
        reloaded = (await db.execute(select(Campaign).where(Campaign.id == cid))).scalar_one()
        assert reloaded.status == CampaignStatus.error
        assert reloaded.scrape_outcome == "scrape_no_account"
