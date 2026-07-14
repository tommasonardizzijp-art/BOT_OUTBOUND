"""Una campagna 'completed' con lo scraping NON finito deve poter ripartire a
raccogliere contatti (listing/bio), senza Reset.

Scenario Tommaso: si mandano i DM ai profili gia' raccolti su lista parziale; a
DM esauriti la campagna si marca 'completed' anche se restano profili da
scrapare. Va bene, PURCHE' si possa riavviare listing/scraping dopo.
"""
import asyncio
import uuid

from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount, AccountStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.campaign_account import CampaignAccount
from app.models.follower import Follower, FollowerStatus
from app.services.campaign_control import resume_campaign_control


def _seed(status, *, pending: int, scrape_cursor=None):
    cid = str(uuid.uuid4())
    aid = str(uuid.uuid4())

    async def _run():
        async with AsyncSessionLocal() as db:
            db.add(Campaign(id=cid, name=f"rc-{cid[:6]}", source_type="scrape",
                            target_username="t", scrape_mode="followers",
                            status=status, scrape_completed_at=None,
                            scrape_cursor=scrape_cursor))
            db.add(InstagramAccount(id=aid, username=f"acc_{aid[:6]}",
                                    encrypted_password="x", status=AccountStatus.active))
            db.add(CampaignAccount(campaign_id=cid, account_id=aid, role="both", is_active=True))
            for i in range(pending):
                db.add(Follower(id=str(uuid.uuid4()), campaign_id=cid,
                                ig_user_id=uuid.uuid4().int % 10_000_000,
                                username=f"p{i}_{cid[:4]}", status=FollowerStatus.pending))
            await db.commit()
    asyncio.run(_run())
    return cid


def _no_redis_block(monkeypatch):
    import app.services.campaign_control as cc

    async def _ok():
        return True
    monkeypatch.setattr(cc, "check_redis_reachable", _ok)

    async def _noop(db):
        return None
    monkeypatch.setattr(cc, "ensure_bot_accepts_work", _noop)


def test_completed_con_profili_da_scrapare_riparte_da_fase_bio(monkeypatch):
    _no_redis_block(monkeypatch)
    cid = _seed(CampaignStatus.completed, pending=5)

    async def _go():
        async with AsyncSessionLocal() as db:
            campaign, _ = await resume_campaign_control(db, cid, by="test", enqueue=False)
            return campaign.status
    assert asyncio.run(_go()) == CampaignStatus.scraping   # riprende la Fase Bio


def test_completed_senza_pending_riparte_dalla_lista_per_nuovi_contatti(monkeypatch):
    """Nessun pending: riparte la Fase Lista -> estrae contatti NUOVI."""
    _no_redis_block(monkeypatch)
    cid = _seed(CampaignStatus.completed, pending=0)

    async def _go():
        async with AsyncSessionLocal() as db:
            campaign, _ = await resume_campaign_control(db, cid, by="test", enqueue=False)
            return campaign.status
    assert asyncio.run(_go()) == CampaignStatus.listing


def test_completed_con_lista_interrotta_riprende_la_lista(monkeypatch):
    """scrape_cursor valorizzato = Fase Lista interrotta a meta': riprende da li'."""
    _no_redis_block(monkeypatch)
    cid = _seed(CampaignStatus.completed, pending=0, scrape_cursor="cursor-abc")

    async def _go():
        async with AsyncSessionLocal() as db:
            campaign, _ = await resume_campaign_control(db, cid, by="test", enqueue=False)
            return campaign.status
    assert asyncio.run(_go()) == CampaignStatus.listing
