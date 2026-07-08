import asyncio, sys, os
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from collections import Counter
from sqlalchemy import select, func, text
from app.config import settings
from app.database import AsyncSessionLocal
from app.models.campaign import Campaign
from app.models.message import Message, MessageStatus


async def main():
    print("=== SICUREZZA / CONFIG ===")
    print("JWT auth attiva (jwt_secret impostato):", bool(settings.jwt_secret))
    print("CORS origins:", settings.cors_origins_list)
    print("auth_trust_forwarded_for:", settings.auth_trust_forwarded_for)

    async with AsyncSessionLocal() as db:
        # blast radius: messaggi inviati da campagne 't'
        t_ids = (await db.execute(
            select(Campaign.id).where(func.lower(Campaign.name) == "t")
        )).scalars().all()
        print(f"\n=== BLAST RADIUS ===")
        print(f"Campagne 't': {len(t_ids)}")
        if t_ids:
            sent = await db.scalar(
                select(func.count(Message.id)).where(
                    Message.campaign_id.in_(t_ids),
                    Message.status == MessageStatus.sent,
                )
            ) or 0
            any_msg = await db.scalar(select(func.count(Message.id)).where(Message.campaign_id.in_(t_ids))) or 0
            print(f"Messaggi TOTALI (qualsiasi stato) da campagne 't': {any_msg}")
            print(f"Messaggi INVIATI (sent) da campagne 't': {sent}")

        # finestra temporale creazione
        rows = (await db.execute(
            select(func.min(Campaign.created_at), func.max(Campaign.created_at))
            .where(func.lower(Campaign.name) == "t")
        )).first()
        print(f"Creazione 't': dalla {rows[0]} alla {rows[1]}")

        # utenti
        print("\n=== UTENTI ===")
        try:
            urows = (await db.execute(text(
                "select email, role, is_active, last_login_at, created_at from users order by created_at"
            ))).all()
            for email, role, act, last, created in urows:
                print(f"  {email}  role={role} active={act} last_login={last} created={created}")
        except Exception as e:
            print("  (tabella users non leggibile:", e, ")")

        # activity_logs nella finestra 2026-07-07 20:00 .. 22:30
        print("\n=== ACTIVITY_LOGS 2026-07-07 20:00..22:30 (per action) ===")
        try:
            arows = (await db.execute(text(
                "select action, count(*) from activity_logs "
                "where created_at between '2026-07-07 20:00' and '2026-07-07 22:30' "
                "group by action order by count(*) desc"
            ))).all()
            if not arows:
                print("  nessun activity_log nella finestra.")
            for action, cnt in arows:
                print(f"  {action}: {cnt}")
        except Exception as e:
            print("  (activity_logs non leggibile:", e, ")")


asyncio.run(main())
