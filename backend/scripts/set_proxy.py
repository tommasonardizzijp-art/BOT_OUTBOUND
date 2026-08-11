"""Assegna (bind) un proxy a uno o piu' account, per-account nel DB.

Uso (dal folder backend, worker acceso o spento e' indifferente):
    ./venv/Scripts/python.exe scripts/set_proxy.py <proxy_url> <username> [username...]
    ./venv/Scripts/python.exe scripts/set_proxy.py --clear      <username> [username...]

Esempi:
    ./venv/Scripts/python.exe scripts/set_proxy.py http://10.165.255.8:8080 5columnbusiness primero_azienda_cbd
    ./venv/Scripts/python.exe scripts/set_proxy.py --clear 5columnbusiness

GUARDIA ANTI-BAN (regola critica PROXY_MOBILE_SETUP.md):
  un account gia' LOGGATO (session_data presente) non va spostato di IP: IG vede lo
  stesso device teletrasportarsi di ASN -> challenge/block immediato. Lo script SALTA
  gli account gia' loggati. Per forzare comunque: variabile d'ambiente FORCE=1
  (e ricordati che dopo un cambio proxy va fatto /reset-session e ri-login sull'IP nuovo).

Scrive SOLO il campo instagram_accounts.proxy. Non tocca sessioni, campagne, job.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount


async def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return
    first = sys.argv[1]
    clear = first == "--clear"
    proxy = None if clear else first
    usernames = sys.argv[2:]
    force = os.environ.get("FORCE") == "1"

    async with AsyncSessionLocal() as db:
        for uname in usernames:
            acct = (await db.execute(
                select(InstagramAccount).where(InstagramAccount.username == uname)
            )).scalar_one_or_none()
            if acct is None:
                print(f"[X] @{uname}: non trovato nel DB — SALTATO")
                continue

            has_session = bool(acct.session_data)
            cur = acct.proxy or "(vuoto)"
            print(f"[..] @{acct.username}: status={acct.status.value}, proxy attuale={cur}, "
                  f"loggato={'si' if has_session else 'no'}")

            if has_session and not force:
                print(f"     [SKIP] gia' loggato: cambiare proxy ora = teletrasporto ASN (block).")
                print(f"            Se e' voluto: reset-session poi ri-login sull'IP nuovo, "
                      f"oppure FORCE=1 per scrivere comunque il campo.")
                continue

            acct.proxy = proxy
            acct.updated_at = datetime.utcnow()
            print(f"     [OK] proxy -> {proxy or '(vuoto)'}"
                  + ("  (FORCED: fai reset-session + ri-login!)" if has_session and force else ""))

        await db.commit()
    print("[done] commit eseguito.")


asyncio.run(main())
