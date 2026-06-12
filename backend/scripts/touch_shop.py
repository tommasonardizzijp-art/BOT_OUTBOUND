import asyncio, sys
from datetime import datetime
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus

CID = "d39d304c-45bf-4f22-b450-1b8d0545eb5b"

async def main():
    async with AsyncSessionLocal() as db:
        c = await db.get(Campaign, CID)
        c.status = CampaignStatus.scraping
        c.scrape_break_until = None
        c.scrape_break_prev_status = None
        c.scrape_outcome = None
        c.updated_at = datetime.utcnow()
        await db.commit()
        print(f"status={c.status.value} updated_at={c.updated_at} (fresh -> startup guard will skip)")

asyncio.run(main())
