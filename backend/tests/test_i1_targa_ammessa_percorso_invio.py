# backend/tests/test_i1_targa_ammessa_percorso_invio.py
"""I1 Important: targa_ammessa_in_anagrafica proteggeva solo upsert_lead
(Task 12). Il dedup anti-doppio-DM passa anche da campaign_orchestrator.
_mark_globally_contacted e da reservation.try_reserve, che non controllavano
il segno di ig_user_id: una targa provvisoria del motore inbox browser
avrebbe potuto prenotare o scrivere nell'anagrafica cross-campagna con una
chiave che la persona raccolta via API (pk vero) non avrebbe mai avuto.
"""
from datetime import datetime, timedelta
import pytest
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.contact_reservation import ContactReservation
from app.models.global_contact import GlobalContact
from app.services import campaign_orchestrator, reservation


@pytest.mark.asyncio
async def test_try_reserve_rifiuta_targa_provvisoria():
    ig_user_id = -998877665544
    async with AsyncSessionLocal() as db:
        ok = await reservation.try_reserve(ig_user_id, "job-1", "camp-1", db)
        assert ok is False

        riga = (await db.execute(
            select(ContactReservation).where(ContactReservation.ig_user_id == ig_user_id)
        )).scalar_one_or_none()
        assert riga is None, "nessuna riga doveva essere scritta in contact_reservations"


@pytest.mark.asyncio
async def test_mark_globally_contacted_rifiuta_targa_provvisoria():
    ig_user_id = -998877665533
    async with AsyncSessionLocal() as db:
        await campaign_orchestrator._mark_globally_contacted(
            ig_user_id, "camp-1", db, campaign_name="Test",
        )

        riga = (await db.execute(
            select(GlobalContact).where(GlobalContact.ig_user_id == ig_user_id)
        )).scalar_one_or_none()
        assert riga is None, "nessuna riga doveva essere scritta in global_contacts"


@pytest.mark.asyncio
async def test_try_reserve_ammette_targa_vera():
    """Non regressione: una targa vera continua a prenotare normalmente."""
    ig_user_id = 998877665511
    async with AsyncSessionLocal() as db:
        try:
            ok = await reservation.try_reserve(ig_user_id, "job-2", "camp-1", db)
            assert ok is True
        finally:
            await db.execute(
                ContactReservation.__table__.delete().where(ContactReservation.ig_user_id == ig_user_id)
            )
            await db.commit()
