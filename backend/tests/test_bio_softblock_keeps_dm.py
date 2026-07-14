"""Un soft-block (429) della Fase Bio non deve spegnere anche i DM.

Caso reale 14/07: 5x HTTP 429 su web_profile_info (endpoint rate-limitato da IG
per IP, a prescindere dal browser) -> _pause_campaign_soft_block metteva l'INTERA
campagna in 'paused' -> il worker DM al giro dopo leggeva status=paused e si
fermava, pur essendo un account diverso e perfettamente sano. Lo scraping deve
fermarsi da solo; i DM devono continuare.
"""
import asyncio
import uuid

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus
from app.services import browser_bio


def _mk(db, status):
    cid = str(uuid.uuid4())
    db.add(Campaign(id=cid, name=f"sb-{cid[:6]}", source_type="scrape",
                    target_username="t", scrape_mode="followers", status=status))
    return cid


async def _status_of(cid):
    from sqlalchemy import select
    async with AsyncSessionLocal() as db:
        c = (await db.execute(select(Campaign).where(Campaign.id == cid))).scalar_one()
        return c.status


def test_softblock_con_dm_in_parallelo_degrada_a_running(monkeypatch):
    """scraping_and_running: ferma solo la bio, i DM restano vivi -> running."""
    cid = {}

    async def _seed():
        async with AsyncSessionLocal() as db:
            cid["id"] = _mk(db, CampaignStatus.scraping_and_running)
            await db.commit()
    asyncio.run(_seed())

    asyncio.run(browser_bio._pause_campaign_soft_block(cid["id"], "acc-1234", 5))

    assert asyncio.run(_status_of(cid["id"])) == CampaignStatus.running


def test_softblock_senza_dm_mette_in_pausa(monkeypatch):
    """scraping puro (nessun DM in corso): resta il comportamento di prima."""
    cid = {}

    async def _seed():
        async with AsyncSessionLocal() as db:
            cid["id"] = _mk(db, CampaignStatus.scraping)
            await db.commit()
    asyncio.run(_seed())

    asyncio.run(browser_bio._pause_campaign_soft_block(cid["id"], "acc-1234", 5))

    assert asyncio.run(_status_of(cid["id"])) == CampaignStatus.paused
