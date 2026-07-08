import asyncio, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount
from app.models.campaign import Campaign
from app.models.campaign_account import CampaignAccount

TARGET = "claudio.abbigliamentovincente"


async def main():
    async with AsyncSessionLocal() as db:
        acc = (await db.execute(
            select(InstagramAccount).where(InstagramAccount.username == TARGET)
        )).scalar_one_or_none()
        if not acc:
            print(f"NESSUN account trovato con username={TARGET!r}")
            return
        print(f"ACCOUNT @{acc.username}")
        print(f"  id={acc.id}")
        print(f"  status={acc.status.value}")
        print(f"  proxy={acc.proxy!r}")
        print(f"  last_login_at={acc.last_login_at} last_activity_at={getattr(acc,'last_activity_at',None)}")
        print(f"  scrape_lookups_today={getattr(acc,'scrape_lookups_today',None)} date={getattr(acc,'scrape_lookups_date',None)}")
        print()
        rows = (await db.execute(
            select(Campaign, CampaignAccount)
            .join(CampaignAccount, CampaignAccount.campaign_id == Campaign.id)
            .where(CampaignAccount.account_id == acc.id)
            .order_by(Campaign.updated_at.desc())
        )).all()
        if not rows:
            print("Nessuna campagna con questo account assegnato.")
            return
        print(f"CAMPAGNE con @{acc.username} assegnato ({len(rows)}):")
        for c, ca in rows:
            print(f"  - {c.name!r}  status={c.status.value}  role={ca.role!r}  is_active={ca.is_active}  campaign_id={c.id}")


asyncio.run(main())
