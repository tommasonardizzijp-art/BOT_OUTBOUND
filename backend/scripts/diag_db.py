import asyncio, os, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from sqlalchemy import select, func
from app.config import settings
from app.database import AsyncSessionLocal
from app.models.campaign import Campaign


def _mask(url: str) -> str:
    # nascondi credenziali: mostra solo schema + host
    try:
        if url.startswith("sqlite"):
            return url
        head, tail = url.split("://", 1)
        if "@" in tail:
            tail = tail.split("@", 1)[1]
        return f"{head}://***@{tail}"
    except Exception:
        return "<unparseable>"


async def main():
    print("DATABASE_URL (bot attivo):", _mask(settings.database_url))
    # file SQLite locale?
    sqlite_path = os.path.abspath("./data/bot.db")
    if os.path.exists(sqlite_path):
        st = os.stat(sqlite_path)
        import datetime as _dt
        print(f"SQLite locale ESISTE: {sqlite_path}")
        print(f"   size={st.st_size} bytes  mtime={_dt.datetime.fromtimestamp(st.st_mtime)}")
    else:
        print(f"SQLite locale NON esiste: {sqlite_path}")

    async with AsyncSessionLocal() as db:
        total = await db.scalar(select(func.count(Campaign.id))) or 0
        t_like = (await db.execute(
            select(Campaign.name, Campaign.status, Campaign.created_at)
            .where(func.lower(Campaign.name).like("t%"))
            .order_by(Campaign.created_at.desc())
        )).all()
        print(f"\nCampagne totali nel DB attivo: {total}")
        # conta esatte 't'
        exact_t = [r for r in t_like if (r[0] or "").strip().lower() == "t"]
        print(f"Campagne con nome che inizia per 't': {len(t_like)}  |  nome == 't' esatto: {len(exact_t)}")
        for name, st, created in t_like[:40]:
            print(f"   name={name!r} status={getattr(st,'value',st)} created_at={created}")


asyncio.run(main())
