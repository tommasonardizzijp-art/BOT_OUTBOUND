# backend/tests/test_i1_targa_ammessa_percorso_invio.py
"""I1 Important: targa_ammessa_in_anagrafica proteggeva solo upsert_lead
(Task 12), poi e' stata estesa a reservation.try_reserve e
campaign_orchestrator._mark_globally_contacted (stesso presidio sui tre
percorsi che scrivono/leggono l'anagrafica cross-campagna).

Questo file inchiodava il comportamento PRE-Task-5: una targa provvisoria
veniva rifiutata su tutti e tre i percorsi. Con Task 5
(2026-08-22-username-chiave-di-prima-classe.md) quel rifiuto e' stato
ribaltato di proposito: `username_norm` (migration 039, UNIQUE) e' il ponte
che fa convergere pk reale e targa provvisoria sulla stessa riga, quindi la
targa negativa non spacca piu' il dedup cross-campagna come temeva il
commento originale. Il presidio resta, ma protegge solo cio' che non e'
affatto una targa: None e zero.

I test qui sotto sono stati riscritti DI PROPOSITO per il nuovo comportamento,
non "aggiustati" per farli passare: il vecchio comportamento che inchiodavano
e' esattamente quello che Task 5 esiste per rimuovere.
"""
from datetime import datetime, timedelta
import pytest
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.contact_reservation import ContactReservation
from app.models.global_contact import GlobalContact
from app.services import campaign_orchestrator, reservation


@pytest.mark.asyncio
async def test_try_reserve_ammette_targa_provvisoria():
    """Post-Task 5: la targa provvisoria (browser) e' una chiave legittima
    quanto un pk reale. Prima di questo lavoro `try_reserve` la rifiutava, il
    contatto finiva `skipped` con motivo "already_contacted_globally", e non
    riceveva MAI il DM — non era una protezione, era un invio che non partiva."""
    ig_user_id = -998877665544
    async with AsyncSessionLocal() as db:
        try:
            ok = await reservation.try_reserve(ig_user_id, "job-1", "camp-1", db)
            assert ok is True

            riga = (await db.execute(
                select(ContactReservation).where(ContactReservation.ig_user_id == ig_user_id)
            )).scalar_one_or_none()
            assert riga is not None, "la prenotazione doveva essere scritta"
        finally:
            await reservation.release(ig_user_id, db)
            await db.commit()


@pytest.mark.asyncio
async def test_mark_globally_contacted_ammette_targa_provvisoria():
    """Stesso ribaltamento sul secondo percorso: una targa provvisoria ora
    entra in global_contacts, con `username_norm` (migration 039) a fare da
    ponte verso la stessa persona vista dal canale API."""
    ig_user_id = -998877665533
    async with AsyncSessionLocal() as db:
        await campaign_orchestrator._mark_globally_contacted(
            ig_user_id, "camp-1", db, campaign_name="Test",
        )

        riga = (await db.execute(
            select(GlobalContact).where(GlobalContact.ig_user_id == ig_user_id)
        )).scalar_one_or_none()
        assert riga is not None, "la riga doveva essere scritta in global_contacts"

        await db.execute(
            GlobalContact.__table__.delete().where(GlobalContact.ig_user_id == ig_user_id)
        )
        await db.commit()


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


@pytest.mark.asyncio
async def test_try_reserve_rifiuta_zero_e_none():
    """Cio' che resta rifiutato: non una targa negativa, ma l'assenza di una
    targa (None) o il valore impossibile (zero)."""
    async with AsyncSessionLocal() as db:
        for ig_user_id in (0, None):
            ok = await reservation.try_reserve(ig_user_id, "job-3", "camp-1", db)
            assert ok is False, f"ig_user_id={ig_user_id!r} non doveva prenotare"
