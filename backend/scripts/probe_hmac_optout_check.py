"""Controllo mirato SOLA LETTURA: per i 9 duplicati, guarda ogni
wa_inbound_events (preview_text, processed) su ENTRAMBI i gemelli. Prima di
decidere quale contatto tenere, bisogna essere sicuri che nessuno dei due
abbia un segnale di opt-out non collegato al contatto che restera' vivo --
e' il gate legale, non si fonde alla cieca."""
import asyncio
from collections import defaultdict

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.wa import WaContact, WaInboundEvent
from app.utils.crypto import decrypt
from app.utils.phone_pseudonym import normalize_e164


async def main():
    async with AsyncSessionLocal() as db:
        contatti = (await db.execute(select(WaContact))).scalars().all()
        gruppi = defaultdict(list)
        for c in contatti:
            try:
                canonico = normalize_e164(decrypt(c.encrypted_phone))
            except Exception:
                continue
            gruppi[(c.tenant_id, canonico)].append(c)
        duplicati = {k: v for k, v in gruppi.items() if len(v) > 1}

        trovati_eventi = False
        for (tenant_id, canonico), righe in duplicati.items():
            for c in righe:
                eventi = (await db.execute(
                    select(WaInboundEvent)
                    .where(WaInboundEvent.contact_id == c.id))).scalars().all()
                if eventi:
                    trovati_eventi = True
                    print(f"numero={canonico}  contatto={c.id}  opted_out={c.opted_out}  do_not_contact={c.do_not_contact}")
                    for e in eventi:
                        print(f"   evento={e.id}  detected_at={e.detected_at}  processed={e.processed}  "
                             f"matched_by={e.matched_by}  preview={e.preview_text!r}")
        if not trovati_eventi:
            print("Nessun wa_inbound_event su nessuno dei 18 contatti duplicati.")


if __name__ == "__main__":
    asyncio.run(main())
