"""Anteprima: che device (telefono simulato) riceverebbe ogni account al PROSSIMO
'Login Browser', con il nuovo pool. NON tocca niente — solo lettura + calcolo.

Uso (dal folder backend):
    ./venv/Scripts/python.exe scripts/preview_devices.py
    ./venv/Scripts/python.exe scripts/preview_devices.py primeroa_adv7 antonino.o.o.54
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount
from app.utils.device_pool import device_for_account


async def main():
    filt = sys.argv[1:]
    async with AsyncSessionLocal() as db:
        accts = (await db.execute(select(InstagramAccount))).scalars().all()
        accts = sorted(accts, key=lambda a: a.username)
        seen = {}
        for a in accts:
            if filt and a.username not in filt:
                continue
            d = device_for_account(a.username)
            tel = f"{d['manufacturer']} {d['model']} (Android {d['android_release']})"
            seen.setdefault(tel, []).append(a.username)
            print(f"@{a.username:34} -> {tel}")
        print("-" * 70)
        dupes = {k: v for k, v in seen.items() if len(v) > 1}
        if dupes:
            print("[!] device CONDIVISI (collisione hash):")
            for tel, us in dupes.items():
                print(f"   {tel}: {', '.join(us)}")
        else:
            print("OK: ogni account ha un telefono distinto.")


asyncio.run(main())
