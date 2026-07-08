import asyncio, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from sqlalchemy import text
from app.database import AsyncSessionLocal


async def main():
    async with AsyncSessionLocal() as db:
        # campagne 't' per created_at raggruppate per ora
        print("=== Campagne 't' create per ORA (07/07) ===")
        rows = (await db.execute(text(
            "select to_char(created_at,'YYYY-MM-DD HH24') as ora, count(*) "
            "from campaigns where lower(name)='t' group by ora order by ora"
        ))).all()
        for ora, cnt in rows:
            print(f"  {ora}:00  -> {cnt}")

        # tutti gli activity_logs 'campaign_created' (quando esistono)
        print("\n=== activity_logs action='campaign_created' (totali) ===")
        c = (await db.execute(text("select count(*) from activity_logs where action='campaign_created'"))).scalar()
        print("  totale righe campaign_created:", c)
        mm = (await db.execute(text(
            "select min(created_at), max(created_at) from activity_logs where action='campaign_created'"
        ))).first()
        print("  finestra campaign_created:", mm[0], "->", mm[1])

        # quanti campaign_created ieri 07/07
        c2 = (await db.execute(text(
            "select count(*) from activity_logs where action='campaign_created' "
            "and created_at::date = '2026-07-07'"
        ))).scalar()
        print("  campaign_created il 2026-07-07:", c2)

        # distinte action nell'intera giornata 07/07
        print("\n=== TUTTE le action di activity_logs il 2026-07-07 ===")
        rows = (await db.execute(text(
            "select action, count(*), min(created_at), max(created_at) from activity_logs "
            "where created_at::date='2026-07-07' group by action order by count(*) desc"
        ))).all()
        for action, cnt, mn, mx in rows:
            print(f"  {action}: {cnt}  ({mn} .. {mx})")


asyncio.run(main())
