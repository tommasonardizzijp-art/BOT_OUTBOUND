"""_scrape_batch: outcome 'blocked' isola l'account e pausa la campagna, non solo
`break` (Task 3, addendum). Il batch gira durante le pause tra sessioni: con solo
`break` un account gia' dietro l'interstiziale verrebbe ripescato dal ciclo
successivo e continuerebbe a generare traffico da dietro il blocco -- esattamente
il fail-mode che questo modulo esiste per chiudere.
"""
from datetime import datetime
import uuid
import pytest

from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount, AccountStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.services import browser_bio


@pytest.mark.asyncio
async def test_scrape_batch_blocked_isolates_account_and_pauses_without_marking(monkeypatch):
    base = 973000000000 + int(datetime.utcnow().timestamp()) % 100000
    acc_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        db.add(InstagramAccount(id=acc_id, username=f"acc_batch_blocked_{base}",
                                encrypted_password="x", status=AccountStatus.active,
                                daily_message_limit=20))
        camp = Campaign(name="t", status=CampaignStatus.scraping, source_type="scrape")
        db.add(camp); await db.flush()
        db.add(Follower(campaign_id=camp.id, ig_user_id=base,
                        username=f"u{base}", status=FollowerStatus.pending))
        await db.commit()
        cid = camp.id

    async def fake_fetch(follower, campaign, db, session):
        return "blocked", Exception("interstiziale IG: scraping_warning")
    monkeypatch.setattr(browser_bio, "fetch_and_store_bio_browser", fake_fetch)

    async with AsyncSessionLocal() as db:
        camp = await db.get(Campaign, cid)
        done = await browser_bio._scrape_batch(
            camp, db, browser_session=None, count=5, account_id=acc_id
        )

    assert done == 0  # nessuna bio estratta: fermato subito sul blocco

    async with AsyncSessionLocal() as db:
        from sqlalchemy import select
        f = (await db.execute(
            select(Follower).where(Follower.campaign_id == cid)
        )).scalar_one()
        assert f.status == FollowerStatus.pending     # NON marcato skipped
        assert f.locked_by_account_id is None          # mai stato lockato dal batch

        c = await db.get(Campaign, cid)
        a = await db.get(InstagramAccount, acc_id)
        assert c.status == CampaignStatus.paused
        assert a.status == AccountStatus.challenge_required
