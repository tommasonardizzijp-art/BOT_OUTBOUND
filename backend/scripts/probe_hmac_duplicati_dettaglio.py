"""Segue probe_hmac_duplicati.py: per ognuno dei 9 gruppi, stampa
campaign_id/status di wa_campaign_contacts e status/sent_at di wa_messages
per ENTRAMBI i gemelli -- serve a capire se la stessa persona ha ricevuto
un messaggio VERO (status=sent) da entrambi i contatti duplicati, che
sarebbe un doppio invio reale, non solo un duplicato a riposo.
SOLA LETTURA."""
import asyncio
from collections import defaultdict

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.wa import WaCampaign, WaCampaignContact, WaContact, WaMessage
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

        for (tenant_id, canonico), righe in duplicati.items():
            print(f"\n{'='*70}\nnumero_canonico={canonico}")
            for c in righe:
                print(f"  contatto={c.id}  chat_title={c.chat_title!r}")
                camps = (await db.execute(
                    select(WaCampaignContact, WaCampaign.name, WaCampaign.status)
                    .join(WaCampaign, WaCampaign.id == WaCampaignContact.campaign_id)
                    .where(WaCampaignContact.contact_id == c.id))).all()
                for wcc, camp_name, camp_status in camps:
                    print(f"     [campagna] {camp_name!r} (status={camp_status})  "
                          f"stato_contatto={wcc.status}  current_step={wcc.current_step}  "
                          f"next_action_at={wcc.next_action_at}  locked_by={wcc.locked_by}")
                msgs = (await db.execute(
                    select(WaMessage.status, WaMessage.sent_at, WaMessage.campaign_id,
                          WaMessage.step_index)
                    .where(WaMessage.contact_id == c.id))).all()
                for status, sent_at, campaign_id, step_index in msgs:
                    print(f"     [messaggio] status={status}  sent_at={sent_at}  "
                          f"campaign_id={campaign_id}  step={step_index}")


if __name__ == "__main__":
    asyncio.run(main())
