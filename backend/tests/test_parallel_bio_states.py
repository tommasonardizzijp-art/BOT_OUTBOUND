"""DM in parallelo durante la Fase Bio (stato scraping_and_running).

Bug: i worker Bio (scrape_bios / browser_bio) uscivano quando la campagna passava
a scraping_and_running (gate ammetteva solo scraping/scraping_break) — cliccare
"Avvia DM ora" fermava lo scraping. Fix: SCRAPING_ACTIVE_STATES include il parallelo,
e a fine Bio si va in 'running' (non 'ready') per tenere vivi i worker DM.
"""
import asyncio
import uuid

import pytest

from app.database import AsyncSessionLocal
from app.models.campaign import (
    Campaign, CampaignStatus, SCRAPING_ACTIVE_STATES, bio_done_status,
)
from app.models.follower import Follower, FollowerStatus
from app.services.browser_bio import _maybe_complete_browser_bio


# ── unit puri: la semantica degli stati ────────────────────────────────────

def test_scraping_active_states_include_parallelo():
    assert CampaignStatus.scraping in SCRAPING_ACTIVE_STATES
    assert CampaignStatus.scraping_break in SCRAPING_ACTIVE_STATES
    assert CampaignStatus.scraping_and_running in SCRAPING_ACTIVE_STATES
    # stati NON attivi per lo scraping
    assert CampaignStatus.ready not in SCRAPING_ACTIVE_STATES
    assert CampaignStatus.running not in SCRAPING_ACTIVE_STATES
    assert CampaignStatus.listing not in SCRAPING_ACTIVE_STATES


def test_bio_done_status_parallelo_resta_running():
    # DM in parallelo → running (worker DM restano vivi)
    assert bio_done_status(CampaignStatus.scraping_and_running) == CampaignStatus.running
    # Bio da sola → ready (attende avvio manuale DM)
    assert bio_done_status(CampaignStatus.scraping) == CampaignStatus.ready
    assert bio_done_status(CampaignStatus.scraping_break) == CampaignStatus.ready


# ── integrazione: completamento Bio browser sul DB di test ─────────────────

def _seed_campaign(status: CampaignStatus, *, n_pending: int, n_bio: int) -> str:
    cid = str(uuid.uuid4())

    async def _seed():
        async with AsyncSessionLocal() as db:
            db.add(Campaign(
                id=cid, name=f"parallel-{status.value}-{cid[:6]}",
                source_type="scrape", target_username="t", scrape_mode="followers",
                bio_engine="browser", status=status, bio_target=None,
            ))
            base = uuid.uuid4().int % 10_000_000
            k = 0
            for _ in range(n_pending):
                k += 1
                db.add(Follower(campaign_id=cid, ig_user_id=base + k,
                                username=f"p{k}_{cid[:4]}", status=FollowerStatus.pending))
            for _ in range(n_bio):
                k += 1
                db.add(Follower(campaign_id=cid, ig_user_id=base + k,
                                username=f"b{k}_{cid[:4]}", status=FollowerStatus.bio_scraped))
            await db.commit()

    asyncio.run(_seed())
    return cid


def _status_of(cid: str) -> CampaignStatus:
    async def _get():
        async with AsyncSessionLocal() as db:
            c = await db.get(Campaign, cid)
            return c.status

    return asyncio.run(_get())


def test_completamento_parallelo_va_in_running():
    # scraping_and_running + zero pending → completa a RUNNING (DM continua), non ready
    cid = _seed_campaign(CampaignStatus.scraping_and_running, n_pending=0, n_bio=3)
    done = asyncio.run(_maybe_complete_browser_bio(cid))
    assert done is True
    assert _status_of(cid) == CampaignStatus.running


def test_completamento_solo_bio_va_in_ready():
    # scraping (no DM parallelo) + zero pending → completa a READY
    cid = _seed_campaign(CampaignStatus.scraping, n_pending=0, n_bio=3)
    done = asyncio.run(_maybe_complete_browser_bio(cid))
    assert done is True
    assert _status_of(cid) == CampaignStatus.ready


def test_pending_residui_non_completa():
    # pending ancora presenti → NON completa, resta in scraping_and_running
    cid = _seed_campaign(CampaignStatus.scraping_and_running, n_pending=2, n_bio=1)
    done = asyncio.run(_maybe_complete_browser_bio(cid))
    assert done is False
    assert _status_of(cid) == CampaignStatus.scraping_and_running
