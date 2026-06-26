"""
One-shot: estrae tutti i contatti (partecipanti 1:1 e gruppi) dalle chat DM
di un account IG, leggendo l'inbox via instagrapi (session restore, no login).

Uso:
    venv\\Scripts\\python.exe extract_chat_contacts.py borderline_agenzia
"""
import asyncio
import csv
import sys
from datetime import datetime

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount
from app.utils.instagrapi_client import login as _login

AMOUNT = 1000  # thread principali da scaricare (paginati da instagrapi)
PENDING = 200  # richieste in sospeso


async def main(username: str) -> None:
    async with AsyncSessionLocal() as db:
        acc = (
            await db.execute(
                select(InstagramAccount).where(InstagramAccount.username == username)
            )
        ).scalar_one_or_none()
        if acc is None:
            print(f"[ERRORE] account @{username} non trovato nel DB")
            return

        print(f"Login (session restore) @{acc.username} ...")
        client = await _login(acc, db, skip_gql_verify=True)
        own_pk = int(client.user_id)

        print(f"Scarico inbox (amount={AMOUNT}) ...")
        threads = await asyncio.to_thread(client.direct_threads, amount=AMOUNT)
        try:
            pending = await asyncio.to_thread(client.direct_pending_inbox, PENDING)
            threads = list(threads) + list(pending)
        except Exception as e:
            print(f"[warn] pending inbox non disponibile: {e}")

        print(f"Thread totali: {len(threads)}")

        rows = []
        seen = set()
        for t in threads:
            others = [u for u in t.users if int(u.pk) != own_pk]
            is_group = len(others) > 1
            for u in others:
                if u.pk in seen:
                    continue
                seen.add(u.pk)
                rows.append({
                    "username": u.username,
                    "full_name": getattr(u, "full_name", "") or "",
                    "pk": u.pk,
                    "is_private": getattr(u, "is_private", ""),
                    "is_verified": getattr(u, "is_verified", ""),
                    "tipo_chat": "gruppo" if is_group else "1:1",
                    "thread_id": t.id,
                })

        out = f"contatti_chat_{username}_{datetime.now():%Y%m%d_%H%M}.csv"
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=[
                "username", "full_name", "pk", "is_private",
                "is_verified", "tipo_chat", "thread_id",
            ])
            w.writeheader()
            w.writerows(rows)

        print(f"\nContatti unici estratti: {len(rows)}")
        print(f"CSV: {out}")


if __name__ == "__main__":
    user = sys.argv[1] if len(sys.argv) > 1 else "borderline_agenzia"
    asyncio.run(main(user))
