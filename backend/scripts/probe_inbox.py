"""Probe inbox: UNA SOLA chiamata direct_v2/inbox su un account, per misurare quanti
thread rende IG per pagina (come il 25 misurato per i follower). Supervisionato:
fa 1 richiesta e ESCE. Niente loop, niente pagina 2, niente scrittura DB.

Uso (dal folder backend):
    ./venv/Scripts/python.exe scripts/probe_inbox.py <account_username>
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount
from app.utils.instagrapi_client import login as _login
from app.services.inbox_source import fetch_inbox_page, extract_thread_participant, _as_users


async def main():
    if len(sys.argv) < 2:
        print("Uso: python scripts/probe_inbox.py <account_username>")
        return
    username = sys.argv[1]
    async with AsyncSessionLocal() as db:
        acct = (await db.execute(
            select(InstagramAccount).where(InstagramAccount.username == username)
        )).scalar_one_or_none()
        if acct is None:
            print(f"[X] account @{username} non trovato")
            return
        print(f"[..] login @{acct.username} (status={acct.status.value}, proxy={acct.proxy or 'nessuno'})")
        try:
            client = await _login(acct, db)
        except Exception as e:
            print(f"[X] login fallito ({type(e).__name__}): {e}")
            print("     -> se e' challenge/sessione morta: rifai 'Login Browser' su questo account.")
            return
        own_pk = int(client.user_id)
        print(f"[..] UNA chiamata direct_v2/inbox (limit richiesto=20) ...")
        try:
            threads, next_cursor, has_older = await asyncio.to_thread(fetch_inbox_page, client, None)
        except Exception as e:
            es = str(e).lower()
            tag = "429/RATE" if ("429" in es or "too many" in es or "rate" in es) else type(e).__name__
            print(f"[X] chiamata inbox fallita ({tag}): {e}")
            return
        one_to_one = sum(1 for t in threads if extract_thread_participant(_as_users(t), own_pk) is not None)
        print("-" * 60)
        print(f"[OK] thread tornati in 1 pagina : {len(threads)}   (limit richiesto = 20)")
        print(f"     di cui 1-a-1 (contatti)    : {one_to_one}")
        print(f"     gruppi/non validi           : {len(threads) - one_to_one}")
        print(f"     has_older (c'e' altro)      : {has_older}")
        print(f"     next_cursor presente        : {bool(next_cursor)}")
        print("-" * 60)
        if len(threads) > 20:
            print(">>> IG rende PIU' di 20 per pagina (il limit non e' il tetto reale)")
        elif len(threads) == 20:
            print(">>> IG rende esattamente 20 (onora il limit)")
        else:
            print(">>> IG rende MENO di 20 (inbox piccolo o tetto piu' basso)")
        print("FATTO — 1 sola chiamata, niente altro eseguito.")


asyncio.run(main())
