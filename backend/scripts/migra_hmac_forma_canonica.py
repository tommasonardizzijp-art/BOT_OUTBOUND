"""Passo 2 dell'AVVIO 12/08 §1: riscrive phone_hmac in wa_contacts e
wa_discovered_chats nella forma canonica (hmac_phone("+" + cifre)), la
stessa gia' usata da wa_numbers e wa_ingest. Va eseguito SOLO dopo il passo
1 (fusione dei duplicati, gia' fatta: 258 -> 249 wa_contacts, 0 duplicati
residui) -- prima della fusione questa riscrittura avrebbe fatto collidere
le due forme sulla UniqueConstraint(tenant_id, phone_hmac).

wa_contacts: encrypted_phone e' l'unica fonte affidabile (phone_hmac vecchio
puo' essere in una delle due forme). wa_discovered_chats: stesso schema,
ma phone_hmac e' NULLABLE (righe senza numero letto, es. gruppi) -- si
saltano, non c'e' niente da migrare.

Uso:
    HMAC_MERGE_DRY_RUN=1 python -m scripts.migra_hmac_forma_canonica
    python -m scripts.migra_hmac_forma_canonica
"""
import asyncio
import os

from sqlalchemy import func, select

from app.database import AsyncSessionLocal
from app.models.wa import WaContact, WaDiscoveredChat
from app.utils.crypto import decrypt
from app.utils.phone_pseudonym import hmac_phone, normalize_e164

DRY_RUN = os.environ.get("HMAC_MERGE_DRY_RUN") == "1"


async def migra_wa_contacts(db):
    print(f"\n{'='*70}\nwa_contacts")
    righe = (await db.execute(select(WaContact))).scalars().all()
    print(f"  totale righe: {len(righe)}")

    cambiate, invariate, falliti = 0, 0, []
    for c in righe:
        try:
            canonico = hmac_phone("+" + normalize_e164(decrypt(c.encrypted_phone)))
        except Exception as e:
            falliti.append((c.id, str(e)))
            continue
        if c.phone_hmac != canonico:
            print(f"  id={c.id}  {c.phone_hmac} -> {canonico}")
            c.phone_hmac = canonico
            cambiate += 1
        else:
            invariate += 1

    print(f"  cambiate={cambiate}  gia_canoniche={invariate}  falliti={len(falliti)}")
    if falliti:
        print("  !! RIGHE NON MIGRABILI (bloccante, fermarsi e guardare a mano):")
        for cid, err in falliti:
            print(f"     {cid}: {err}")
        return False
    return True


async def migra_wa_discovered_chats(db):
    print(f"\n{'='*70}\nwa_discovered_chats")
    righe = (await db.execute(select(WaDiscoveredChat))).scalars().all()
    print(f"  totale righe: {len(righe)}")

    cambiate, invariate, senza_numero, falliti = 0, 0, 0, []
    for r in righe:
        if r.phone_hmac is None or r.encrypted_phone is None:
            senza_numero += 1
            continue
        try:
            canonico = hmac_phone("+" + normalize_e164(decrypt(r.encrypted_phone)))
        except Exception as e:
            falliti.append((r.id, str(e)))
            continue
        if r.phone_hmac != canonico:
            r.phone_hmac = canonico
            cambiate += 1
        else:
            invariate += 1

    print(f"  cambiate={cambiate}  gia_canoniche={invariate}  senza_numero={senza_numero}  falliti={len(falliti)}")
    if falliti:
        print("  !! RIGHE NON MIGRABILI (bloccante, fermarsi e guardare a mano):")
        for rid, err in falliti:
            print(f"     {rid}: {err}")
        return False
    return True


async def main():
    print(f"MODALITA': {'DRY RUN (nessuna scrittura sopravvive)' if DRY_RUN else 'SCRITTURA REALE'}")
    async with AsyncSessionLocal() as db:
        ok1 = await migra_wa_contacts(db)
        ok2 = await migra_wa_discovered_chats(db)

        if not (ok1 and ok2):
            await db.rollback()
            print("\nSTOP: righe non migrabili trovate, rollback, nessuna scrittura persistita.")
            return

        if DRY_RUN:
            await db.rollback()
            print("\nDRY RUN: rollback finale eseguito, nessuna scrittura persistita.")
        else:
            await db.commit()
            n_contatti_unici = await db.scalar(
                select(func.count(func.distinct(WaContact.phone_hmac))))
            n_contatti = await db.scalar(select(func.count()).select_from(WaContact))
            print(f"\nDOPO commit: wa_contacts={n_contatti}  phone_hmac_distinti={n_contatti_unici} "
                 f"(devono coincidere: nessuna collisione)")


if __name__ == "__main__":
    asyncio.run(main())
