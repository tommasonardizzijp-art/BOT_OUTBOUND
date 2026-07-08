import asyncio, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from sqlalchemy import text
from app.database import AsyncSessionLocal


async def main():
    async with AsyncSessionLocal() as db:
        print("=== Campagne NON-'t' create/aggiornate il 2026-07-07 e 07-08 ===")
        rows = (await db.execute(text(
            "select name, status, source_type, created_at, updated_at from campaigns "
            "where lower(name) <> 't' and (created_at::date in ('2026-07-07','2026-07-08') "
            "   or updated_at::date in ('2026-07-07','2026-07-08')) "
            "order by created_at desc"
        ))).all()
        for name, st, src, cr, up in rows:
            print(f"  {name!r:32} status={st:14} src={src:7} created={cr} updated={up}")

        print("\n=== 'DM Claudio x AV' dettaglio ===")
        rows = (await db.execute(text(
            "select id, name, status, created_at, updated_at from campaigns where name ilike '%claudio%'"
        ))).all()
        for cid, name, st, cr, up in rows:
            print(f"  {cid}  {name!r} status={st} created={cr} updated={up}")

        # 't' campaigns: campi comuni (fingerprint di script)
        print("\n=== 't' fingerprint (valori distinti dei campi chiave) ===")
        for col in ("source_type", "scrape_mode", "target_username", "bio_engine", "messaging_enabled"):
            rows = (await db.execute(text(
                f"select {col}, count(*) from campaigns where lower(name)='t' group by {col}"
            ))).all()
            print(f"  {col}: " + ", ".join(f"{v!r}={c}" for v, c in rows))


asyncio.run(main())
