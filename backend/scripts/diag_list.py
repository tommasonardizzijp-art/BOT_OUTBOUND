import asyncio, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from sqlalchemy import select, func
from app.database import AsyncSessionLocal
from app.models.campaign import Campaign
from app.models.follower import Follower


async def main():
    async with AsyncSessionLocal() as db:
        camps = (await db.execute(
            select(Campaign).where(Campaign.source_type == "scrape").order_by(Campaign.updated_at.desc())
        )).scalars().all()
        for c in camps:
            cnt = await db.scalar(select(func.count(Follower.id)).where(Follower.campaign_id == c.id)) or 0
            print(f"id={c.id} name={c.name!r}")
            print(f"  status={c.status.value} list_target={c.list_target} total_followers={c.total_followers} "
                  f"db_followers={cnt} scrape_cursor={c.scrape_cursor!r} target_username={c.target_username}")

asyncio.run(main())
