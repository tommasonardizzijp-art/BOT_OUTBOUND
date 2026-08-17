"""Check urgente, SOLA LETTURA: dei 9 duplicati, 5 hanno un gemello ancora
'queued' proprio nella campagna PRIMERO FULVIO che sta inviando ORA. Guarda
se nel frattempo sono gia' stati inviati, e mostra lo stato fresco."""
import asyncio

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.wa import WaCampaignContact, WaContact, WaContactStatus, WaMessage

IDS_QUEUED_DUPLICATI = [
    ("Giulia Primero Acilia", "d318577f-ac03-4127-a8ed-10fde8339d1e"),
    ("Michele Venditore", "dc504015-3d65-4eea-af1f-3db51f83732c"),
    ("Borderline", "900033cf-895e-427d-934b-00cf76595516"),
    ("SIMONE (emoji)", "c952ee77-7f89-4112-af18-6d4ce9adf5cc"),
    ("Primero Magazzino", "f09b850e-c3be-43c7-b878-1e53ee4851e1"),
]
CAMPAIGN_ID = "6525a206-d554-4a32-ac13-a289de04ad54"


async def main():
    async with AsyncSessionLocal() as db:
        for label, contact_id in IDS_QUEUED_DUPLICATI:
            wcc = await db.scalar(
                select(WaCampaignContact)
                .where(WaCampaignContact.campaign_id == CAMPAIGN_ID,
                      WaCampaignContact.contact_id == contact_id))
            msg = (await db.execute(
                select(WaMessage.status, WaMessage.sent_at)
                .where(WaMessage.campaign_id == CAMPAIGN_ID, WaMessage.contact_id == contact_id))).all()
            print(f"{label:25s} contact_id={contact_id}  stato_ora={wcc.status if wcc else 'ASSENTE'}  "
                 f"next_action_at={wcc.next_action_at if wcc else '-'}  messaggi={msg}")


if __name__ == "__main__":
    asyncio.run(main())
