"""Cold-ping: UNA sola lookup su UN account, per capire se il throttle IG e' sciolto
PRIMA di lanciare una campagna (che ne brucerebbe 4 in parallelo).

Uso (dal folder backend, worker fermo o acceso e' indifferente):
    ./venv/Scripts/python.exe scripts/cold_ping.py <account_username> [target_username]

Esempi:
    ./venv/Scripts/python.exe scripts/cold_ping.py primeroa_adv7
    ./venv/Scripts/python.exe scripts/cold_ping.py antonino.o.o.54 nike

Cosa fa: login dell'account (come il bot: restore sessione + verify web GQL) poi UNA
chiamata `user_info_by_username_v1` sul target (stessa classe della bio /users/.../info/).
- Stampa OK + follower count  -> il canale mobile risponde, throttle sciolto.
- Stampa 429 / too many        -> ancora throttlato: NON lanciare la campagna, aspetta.

NON scrive nel DB (a parte l'update sessione che fa il login), NON tocca campagne,
NON accoda job. E' una singola lettura.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount
from app.utils.instagrapi_client import login


async def main():
    if len(sys.argv) < 2:
        print("Uso: python scripts/cold_ping.py <account_username> [target_username]")
        return
    account_username = sys.argv[1]
    target = sys.argv[2] if len(sys.argv) > 2 else "instagram"

    async with AsyncSessionLocal() as db:
        acct = (await db.execute(
            select(InstagramAccount).where(InstagramAccount.username == account_username)
        )).scalar_one_or_none()
        if acct is None:
            print(f"[X] account @{account_username} non trovato nel DB")
            return
        print(f"[..] login @{acct.username} (status={acct.status.value}, proxy={acct.proxy or 'nessuno'})")
        try:
            client = await login(acct, db)
        except Exception as e:
            print(f"[X] login fallito: {type(e).__name__}: {e}")
            return
        print(f"[..] 1 lookup su @{target} (user_info_by_username_v1) ...")
        try:
            user = await asyncio.to_thread(client.user_info_by_username_v1, target)
            print(f"[OK] @{target}: follower={user.follower_count}, pk={user.pk} "
                  f"-> canale mobile RISPONDE, throttle sciolto")
        except Exception as e:
            es = str(e).lower()
            if "429" in es or "too many" in es or "rate" in es:
                print(f"[429] ANCORA THROTTLATO: {e}\n     -> NON lanciare la campagna, aspetta ancora.")
            else:
                print(f"[?] altro errore (non 429): {type(e).__name__}: {e}")


asyncio.run(main())
