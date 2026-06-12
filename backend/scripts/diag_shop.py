import asyncio, sys
from collections import Counter
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from sqlalchemy import select, func
from app.database import AsyncSessionLocal
from app.models.campaign import Campaign
from app.models.follower import Follower
from app.models.account import InstagramAccount
from app.models.campaign_account import CampaignAccount


async def main():
    async with AsyncSessionLocal() as db:
        camp = (await db.execute(select(Campaign).where(Campaign.name.ilike("%survivor%")))).scalars().first()
        if not camp:
            print("NO campaign matching survivor"); return
        print(f"campaign id={camp.id} name={camp.name!r}")
        print(f"  status={camp.status.value} source_type={camp.source_type} messaging_enabled={camp.messaging_enabled}")
        print(f"  scrape_mode={camp.scrape_mode} bio_target={camp.bio_target} list_target={camp.list_target}")
        print(f"  scrape_daily_limit={camp.scrape_daily_limit} scrape_break_until={camp.scrape_break_until} prev_status={camp.scrape_break_prev_status}")
        # follower status breakdown
        rows = (await db.execute(select(Follower.status, func.count()).where(Follower.campaign_id==camp.id).group_by(Follower.status))).all()
        print("  followers:", {s.value if hasattr(s,'value') else s: n for s, n in rows})
        # accounts assigned
        cas = (await db.execute(select(CampaignAccount).where(CampaignAccount.campaign_id==camp.id))).scalars().all()
        print(f"  assigned accounts: {len(cas)}")
        for ca in cas:
            acc = await db.get(InstagramAccount, ca.account_id)
            print(f"    @{acc.username} role={ca.role} active={ca.is_active} acc_status={acc.status.value} "
                  f"lookups_today={acc.scrape_lookups_today} lookups_date={acc.scrape_lookups_date} proxy={'Y' if acc.proxy else 'N'}")

asyncio.run(main())
