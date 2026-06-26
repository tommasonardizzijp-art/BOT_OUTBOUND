"""
One-shot: crea una campagna di genere 'dm_threads' (estrazione contatti inbox,
NO invio DM) e ci salva dentro i contatti gia' estratti dal CSV.

Riusa il CSV prodotto da extract_chat_contacts.py -> nessuna nuova chiamata IG.

Uso:
    venv\\Scripts\\python.exe save_inbox_campaign.py <account_username> <csv_path> ["Nome campagna"]
"""
import asyncio
import csv
import sys
from datetime import datetime

from sqlalchemy import select, func

from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount
from app.models.campaign import Campaign, CampaignStatus
from app.models.campaign_account import CampaignAccount
from app.models.follower import Follower, FollowerStatus


def _b(v: str) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes")


async def main(username: str, csv_path: str, camp_name: str) -> None:
    async with AsyncSessionLocal() as db:
        acc = (await db.execute(
            select(InstagramAccount).where(InstagramAccount.username == username)
        )).scalar_one_or_none()
        if acc is None:
            print(f"[ERRORE] account @{username} non trovato")
            return

        # Idempotenza: se esiste gia' una campagna con questo nome, riusala.
        camp = (await db.execute(
            select(Campaign).where(Campaign.name == camp_name)
        )).scalar_one_or_none()
        if camp is None:
            camp = Campaign(
                name=camp_name,
                source_type="scrape",
                scrape_mode="dm_threads",   # genere: estrazione contatti dall'inbox DM
                inbox_engine="api",
                messaging_enabled=False,    # NESSUN invio DM, nessuna generazione AI
                status=CampaignStatus.ready,  # lista pronta (estrazione completata)
                base_message_template=None,
                scrape_cursor=None,
            )
            db.add(camp)
            await db.flush()  # assegna camp.id
            print(f"Campagna creata: '{camp_name}' (id {camp.id})")
        else:
            print(f"Campagna gia' esistente, riuso: '{camp_name}' (id {camp.id})")

        # Link account (role='scraping': sola estrazione, niente DM)
        link = (await db.execute(
            select(CampaignAccount).where(
                CampaignAccount.campaign_id == camp.id,
                CampaignAccount.account_id == acc.id,
            )
        )).scalar_one_or_none()
        if link is None:
            db.add(CampaignAccount(
                campaign_id=camp.id,
                account_id=acc.id,
                role="scraping",
                is_active=True,
            ))
            print(f"Account @{username} collegato (role=scraping)")

        # ig_user_id gia' presenti nella campagna -> dedup
        existing = set((await db.execute(
            select(Follower.ig_user_id).where(Follower.campaign_id == camp.id)
        )).scalars().all())

        inserted = 0
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                try:
                    pk = int(row["pk"])
                except (KeyError, ValueError):
                    continue
                if pk in existing:
                    continue
                existing.add(pk)
                db.add(Follower(
                    campaign_id=camp.id,
                    ig_user_id=pk,
                    username=row.get("username") or "",
                    full_name=(row.get("full_name") or None),
                    is_private=_b(row.get("is_private", "")),
                    is_verified=_b(row.get("is_verified", "")),
                    status=FollowerStatus.pending,
                ))
                inserted += 1

        total = await db.scalar(
            select(func.count(Follower.id)).where(Follower.campaign_id == camp.id)
        )
        camp.total_followers = total or 0
        camp.updated_at = datetime.utcnow()
        await db.commit()

        print(f"\nContatti inseriti: {inserted}")
        print(f"Totale follower in campagna: {camp.total_followers}")
        print(f"messaging_enabled={camp.messaging_enabled} | status={camp.status.value} | scrape_mode={camp.scrape_mode}")


if __name__ == "__main__":
    user = sys.argv[1] if len(sys.argv) > 1 else "borderline_agenzia"
    path = sys.argv[2]
    name = sys.argv[3] if len(sys.argv) > 3 else "DM borderline_agenzia"
    asyncio.run(main(user, path, name))
