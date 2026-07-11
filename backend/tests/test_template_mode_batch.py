"""Batch di generazione in modalità no-AI: nessuna chiamata AI, variante registrata.

Nota fixture: il conftest.py di questo repo NON definisce una fixture `db_session`
(verificato via grep) — i test esistenti che seedano dati per le funzioni batch
usano `AsyncSessionLocal` direttamente (es. test_claim_next_pending.py). Si segue
qui lo stesso pattern.
"""
import pytest
from unittest.mock import patch

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.models.message import Message


@pytest.mark.asyncio
async def test_batch_no_ai_generates_without_ai_client():
    async with AsyncSessionLocal() as db:
        camp = Campaign(
            name="tpl", status=CampaignStatus.ready,
            base_message_template="{Ciao|Hey} {nome}, ti va una collaborazione?",
            message_template_c="Buongiorno {nome}! Due righe veloci sul progetto",
            ai_enabled=False, messaging_enabled=True,
        )
        db.add(camp)
        await db.flush()
        camp_id = camp.id
        for i in range(8):
            db.add(Follower(
                campaign_id=camp_id, ig_user_id=950_000_100 + i,
                username=f"user{i}", full_name=f"Utente {i}",
                biography="bio", status=FollowerStatus.bio_scraped,
            ))
        await db.commit()

    from app.services import ai_personalizer
    with patch.object(ai_personalizer, "get_ai_client") as boom:
        boom.side_effect = AssertionError("AI client chiamato in modalità no-AI!")
        count = await ai_personalizer.generate_messages_batch(camp_id)

    assert count == 8
    async with AsyncSessionLocal() as db:
        msgs = (await db.execute(
            select(Message).where(Message.campaign_id == camp_id)
        )).scalars().all()
    assert len(msgs) == 8
    assert all(m.template_variant in ("a", "c") for m in msgs)
    assert all("{" not in m.generated_text for m in msgs)
    assert any("Utente" in m.generated_text for m in msgs)
