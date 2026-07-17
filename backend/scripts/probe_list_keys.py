"""Probe v2 fine-lista: IG non rende piu' next_max_id sull'endpoint followers
(probe_list_tail.py: 50 utenti senza cursore anche in prima pagina, max_id
apparentemente ignorato). Questo probe distingue le ipotesi:

  1. big_list=false / campo pagination diverso -> dump delle CHIAVI raw della risposta
  2. max_id ignorato -> confronto dei pk tra chiamata da-inizio e chiamata max_id=6800
  3. per-account o globale -> ripete su un secondo account

4 richieste totali (2 per account), delay 10-15s, nessuna scrittura DB.

Uso (dal folder backend):
    ./venv/Scripts/python.exe scripts/probe_list_keys.py <account1> [account2]
"""
import asyncio
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount
from app.models.campaign import Campaign
from app.utils.instagrapi_client import login as _login

CAMPAIGN_ID = "d39d304c-45bf-4f22-b450-1b8d0545eb5b"  # shop survivor


def _fetch_raw(client, user_id, count, max_id):
    return client.private_request(
        f"friendships/{user_id}/followers/",
        params={
            "max_id": max_id or "",
            "count": count,
            "rank_token": client.rank_token,
            "search_surface": "follow_list_page",
            "query": "",
            "enable_groups": "true",
        },
    )


def _describe(tag, result):
    users = result.get("users") or []
    pks = [str(u.get("pk")) for u in users]
    meta = {k: v for k, v in result.items() if k != "users"}
    # tronca valori lunghi ma mostra tutte le chiavi non-users
    meta_repr = {k: (repr(v)[:80] if not isinstance(v, (int, bool, float, type(None))) else v)
                 for k, v in meta.items()}
    print(f"[{tag}] utenti: {len(users)}")
    print(f"[{tag}] chiavi risposta (no users): {meta_repr}")
    print(f"[{tag}] primi 5 pk: {pks[:5]}")
    return set(pks)


async def _delay():
    d = random.uniform(10, 15)
    print(f"     ... delay {d:.0f}s")
    await asyncio.sleep(d)


async def probe_account(db, username, uid):
    acct = (await db.execute(
        select(InstagramAccount).where(InstagramAccount.username == username)
    )).scalar_one_or_none()
    if acct is None:
        print(f"[X] account @{username} non trovato")
        return
    print(f"\n########## @{username} (proxy={acct.proxy or 'nessuno'}) ##########")
    try:
        client = await _login(acct, db)
    except Exception as e:
        print(f"[X] login fallito ({type(e).__name__}): {e}")
        return

    try:
        r1 = await asyncio.to_thread(_fetch_raw, client, uid, 25, None)
    except Exception as e:
        print(f"[X] chiamata da-inizio fallita ({type(e).__name__}): {e}")
        return
    pks1 = _describe("inizio", r1)

    await _delay()
    try:
        r2 = await asyncio.to_thread(_fetch_raw, client, uid, 25, "6800")
    except Exception as e:
        print(f"[X] chiamata max_id=6800 fallita ({type(e).__name__}): {e}")
        return
    pks2 = _describe("max_id=6800", r2)

    inter = len(pks1 & pks2)
    print(f">>> pk in comune inizio vs max_id=6800: {inter}/{len(pks1)} "
          f"({'max_id IGNORATO (stesso blocchetto)' if inter > len(pks1) * 0.8 else 'blocchi DIVERSI (max_id onorato)'})")


async def main():
    if len(sys.argv) < 2:
        print("Uso: python scripts/probe_list_keys.py <account1> [account2]")
        return
    accounts = sys.argv[1:3]
    async with AsyncSessionLocal() as db:
        campaign = (await db.execute(
            select(Campaign).where(Campaign.id == CAMPAIGN_ID)
        )).scalar_one_or_none()
        uid = campaign.target_user_id
        print(f"[..] target @{campaign.target_username} (uid {uid})")
        for i, username in enumerate(accounts):
            if i > 0:
                await _delay()
            await probe_account(db, username, uid)
        print("\nFATTO — nessuna scrittura DB.")


asyncio.run(main())
