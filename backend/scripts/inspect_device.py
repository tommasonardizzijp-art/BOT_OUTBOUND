"""Dump del device simulato + fingerprint sessione per uno o piu' account."""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount

USERNAMES = sys.argv[1:] or ["primero_azienda_cbd", "primeroa_adv7"]


def show(acct):
    print("=" * 78)
    print(f"@{acct.username}   status={acct.status.value}   proxy={acct.proxy or 'NESSUNO'}")
    if not acct.session_data:
        print("  (nessuna session_data)")
        return None
    s = json.loads(acct.session_data)
    ds = s.get("device_settings", {}) or {}
    uu = s.get("uuids", {}) or {}
    print(f"  user_agent   : {s.get('user_agent')}")
    print(f"  app_version  : {ds.get('app_version')}   version_code={ds.get('version_code')}")
    print(f"  device       : {ds.get('manufacturer')} / {ds.get('model')} / {ds.get('device')}")
    print(f"  android      : rel={ds.get('android_release')} ver={ds.get('android_version')} "
          f"cpu={ds.get('cpu')} dpi={ds.get('dpi')} res={ds.get('resolution')}")
    print(f"  locale/tz    : {s.get('locale')} / {s.get('country')} / tz={s.get('timezone_offset')}")
    print(f"  android_device_id : {uu.get('android_device_id')}")
    print(f"  phone_id     : {uu.get('phone_id')}")
    print(f"  last_login   : {s.get('last_login')}  (None = mai un login mobile, sessione web-born)")
    # firma HARDWARE (esclude app_version/user_agent): se identica tra account =
    # stesso modello di telefono simulato = device di default instagrapi condiviso.
    sig = (ds.get("manufacturer"), ds.get("model"), ds.get("device"), ds.get("cpu"),
           ds.get("dpi"), ds.get("resolution"), ds.get("android_release"), ds.get("android_version"))
    return sig


async def main():
    async with AsyncSessionLocal() as db:
        sigs = {}
        for un in USERNAMES:
            acct = (await db.execute(
                select(InstagramAccount).where(InstagramAccount.username == un)
            )).scalar_one_or_none()
            if acct is None:
                print(f"@{un}: NON trovato")
                continue
            sigs[un] = show(acct)
        print("=" * 78)
        present = [v for v in sigs.values() if v is not None]
        uniq = set(present)
        if len(uniq) <= 1 and len(present) > 1:
            print(f">>> STESSO modello HARDWARE su tutti ({len(present)} account) = "
                  f"device di default instagrapi CONDIVISO (tell per il cluster)")
            print(f"    hardware: {list(uniq)[0]}")
        else:
            print(f">>> {len(uniq)} modelli hardware distinti su {len(present)} account")


asyncio.run(main())
