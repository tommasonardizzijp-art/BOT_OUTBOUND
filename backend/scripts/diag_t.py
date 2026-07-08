import asyncio, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from collections import Counter
from sqlalchemy import select, func
from app.database import AsyncSessionLocal
from app.models.campaign import Campaign
from app.models.campaign_account import CampaignAccount
from app.models.follower import Follower
from app.models.message import Message


async def main():
    async with AsyncSessionLocal() as db:
        camps = (await db.execute(
            select(Campaign).where(func.lower(Campaign.name).like("t%")).order_by(Campaign.created_at.desc())
        )).scalars().all()
        camps = [c for c in camps if (c.name or "").strip().lower() == "t"]
        print(f"Campagne 't': {len(camps)}")
        by_status = Counter(c.status.value for c in camps)
        print("Per stato:", dict(by_status))

        # stati che BLOCCANO la delete (da campaigns.delete_campaign)
        blocking = {"running", "listing", "listing_break", "scraping", "scraping_and_running", "scraping_break"}
        n_block = sum(1 for c in camps if c.status.value in blocking)
        print(f"In stato che BLOCCA la delete (400): {n_block}")

        # figli FK sui primi 8
        print("\nFigli FK (prime 8):")
        for c in camps[:8]:
            fa = await db.scalar(select(func.count(CampaignAccount.id)).where(CampaignAccount.campaign_id == c.id)) or 0
            ff = await db.scalar(select(func.count(Follower.id)).where(Follower.campaign_id == c.id)) or 0
            fm = await db.scalar(select(func.count(Message.id)).where(Message.campaign_id == c.id)) or 0
            print(f"  {c.id[:8]} status={c.status.value:16} accounts={fa} followers={ff} messages={fm}")


asyncio.run(main())
