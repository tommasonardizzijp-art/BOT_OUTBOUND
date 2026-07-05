"""SPIKE (non codice di produzione): verifica full_detail_info su un account reale.

Decide il ramo del Task 3 del piano anti-detection:
  - ramo A: se full_detail_info risponde 200 e contiene user con i campi contatto.
  - ramo B (default sicuro): altrimenti user_info_v1 app-like + user_medias_v1.

Uso (backend/, venv attivo):
    ./venv/Scripts/python.exe -m scripts.spike_full_detail_info <account_id> <target_pk>

<account_id> = id di un InstagramAccount con sessione valida (login browser fatto).
<target_pk>  = pk numerico di un profilo target reale (es. un follower gia' in DB).
Stampa status/chiavi e se i campi contatto ci sono; dump completo in scratch_full_detail_info.json.
"""
import asyncio
import json
import sys

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount
from app.utils.instagrapi_client import login


async def main(account_id: str, target_pk: str) -> None:
    async with AsyncSessionLocal() as db:
        acc = (await db.execute(
            select(InstagramAccount).where(InstagramAccount.id == account_id)
        )).scalar_one_or_none()
        if acc is None:
            print(f"Account {account_id} non trovato.")
            return
        client = await login(acc, db)

    def _call():
        return client.private_request(f"users/{target_pk}/full_detail_info/")

    try:
        result = await asyncio.to_thread(_call)
    except Exception as e:
        print(f"ERRORE chiamata full_detail_info: {type(e).__name__}: {e}")
        print("=> RAMO B (fallback user_info_v1 app-like + user_medias_v1).")
        return

    print("TOP-LEVEL KEYS:", list(result.keys()))
    user = (result.get("user_detail") or {}).get("user") or result.get("user") or {}
    print("HAS biography:", "biography" in user)
    print("public_email:", user.get("public_email"))
    print("public_phone_number:", user.get("public_phone_number"))
    print("external_url:", user.get("external_url"))
    print("HAS feed/media:", bool(result.get("feed") or result.get("reels_media")))

    with open("scratch_full_detail_info.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    print("Dump completo -> scratch_full_detail_info.json")

    ok = bool(user) and "biography" in user
    print("=> RAMO A (usa full_detail_info)" if ok else "=> RAMO B (fallback)")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Uso: python -m scripts.spike_full_detail_info <account_id> <target_pk>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1], sys.argv[2]))
