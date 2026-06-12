"""Regressione: la Fase Bio non deve ciclare all'infinito su un profilo che
fallisce la lookup (es. instagrapi KeyError 'pinned_channels_info').

Bug originale: scrape_bios gestiva solo capped/challenge/soft_block/done.
Un outcome 'error' lasciava il follower a status=pending; il loop lo ri-selezionava
con limit(1) (deterministico) -> loop infinito senza delay.

Questi test pilotano il loop reale di scrape_bios con un fetch_and_store_bio finto:
  - tutte 'error'  -> i follower vengono skippati, la run termina (status ready)
  - tutte 'network' -> la run si mette in pausa (error) SENZA bruciare i pending
"""
import asyncio
import tempfile
import os
import uuid
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.database import Base
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
import app.models.account  # noqa: F401
import app.models.campaign_account  # noqa: F401
import app.models.message  # noqa: F401
import app.models.activity_log  # noqa: F401
import app.models.global_contact  # noqa: F401

from app.services import scrape_bios


class _FakePool:
    @classmethod
    async def build(cls, db, campaign):
        return cls()

    def next(self, campaign):
        return ("fake_account", "fake_client")

    async def save_sessions(self, db):
        pass

    async def release(self):
        pass


def _setup(monkeypatch, outcome):
    """Wire scrape_bios contro un DB sqlite temporaneo + dipendenze finte.
    Ritorna (session_factory, campaign_id, cleanup)."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="bioloop_")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", connect_args={"check_same_thread": False})
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    campaign_id = str(uuid.uuid4())

    async def _seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_factory() as db:
            db.add(Campaign(
                id=campaign_id,
                name="bio loop test",
                source_type="scrape",
                target_username="target",
                status=CampaignStatus.scraping,
                messaging_enabled=False,
            ))
            for i in range(3):
                db.add(Follower(
                    id=str(uuid.uuid4()),
                    campaign_id=campaign_id,
                    ig_user_id=900000000 + i,
                    username=f"user_{i}",
                    status=FollowerStatus.pending,
                ))
            await db.commit()

    asyncio.run(_seed())

    # Patch dipendenze esterne del loop.
    monkeypatch.setattr(scrape_bios, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(scrape_bios, "ScrapingPool", _FakePool)

    async def _not_halted(db):
        return False
    monkeypatch.setattr(scrape_bios, "is_halted", _not_halted)

    err = KeyError("pinned_channels_info")

    async def _fetch(follower, campaign, db, pool):
        # NON cambia lo status del follower (resta pending): e' il loop che deve
        # decidere come avanzare.
        return outcome, "fake_account", err
    monkeypatch.setattr(scrape_bios, "fetch_and_store_bio", _fetch)

    monkeypatch.setattr("app.utils.events.emit", lambda *a, **k: None)

    # Niente attese reali nel test.
    _real_sleep = asyncio.sleep

    async def _fast_sleep(_):
        await _real_sleep(0)
    monkeypatch.setattr(scrape_bios.asyncio, "sleep", _fast_sleep)
    monkeypatch.setattr(scrape_bios.random, "uniform", lambda a, b: 0)

    def cleanup():
        asyncio.run(engine.dispose())
        try:
            os.remove(path)
        except OSError:
            pass

    return session_factory, campaign_id, cleanup


def test_error_outcome_skips_and_terminates(monkeypatch):
    """Outcome 'error' su tutti i follower: niente loop infinito, vengono skippati,
    campagna torna ready."""
    session_factory, campaign_id, cleanup = _setup(monkeypatch, "error")
    try:
        async def _go():
            # wait_for: se il loop e' infinito (bug), il test fallisce per timeout
            # invece di appendere il processo.
            await asyncio.wait_for(scrape_bios.scrape_bios(campaign_id), timeout=10)

            async with session_factory() as db:
                from sqlalchemy import select, func
                skipped = await db.scalar(select(func.count(Follower.id)).where(
                    Follower.campaign_id == campaign_id,
                    Follower.status == FollowerStatus.skipped,
                ))
                pending = await db.scalar(select(func.count(Follower.id)).where(
                    Follower.campaign_id == campaign_id,
                    Follower.status == FollowerStatus.pending,
                ))
                campaign = await db.get(Campaign, campaign_id)
                return skipped, pending, campaign.status

        skipped, pending, status = asyncio.run(_go())
        assert skipped == 3, f"attesi 3 follower skippati, trovati {skipped}"
        assert pending == 0, f"nessun pending atteso, trovati {pending}"
        assert status == CampaignStatus.ready
    finally:
        cleanup()


def test_network_outcome_pauses_without_burning_pending(monkeypatch):
    """Outcome 'network' (connessione giu', es. tethering USB staccato):
    la run si ferma in error e i follower restano pending per il resume."""
    session_factory, campaign_id, cleanup = _setup(monkeypatch, "network")
    try:
        async def _go():
            await asyncio.wait_for(scrape_bios.scrape_bios(campaign_id), timeout=10)
            async with session_factory() as db:
                from sqlalchemy import select, func
                pending = await db.scalar(select(func.count(Follower.id)).where(
                    Follower.campaign_id == campaign_id,
                    Follower.status == FollowerStatus.pending,
                ))
                skipped = await db.scalar(select(func.count(Follower.id)).where(
                    Follower.campaign_id == campaign_id,
                    Follower.status == FollowerStatus.skipped,
                ))
                campaign = await db.get(Campaign, campaign_id)
                return pending, skipped, campaign.status, campaign.scrape_outcome

        pending, skipped, status, outcome = asyncio.run(_go())
        assert pending == 3, f"i pending non vanno bruciati su errore di rete, trovati {pending}"
        assert skipped == 0
        assert status == CampaignStatus.error
        assert outcome == "scrape_network_error"
    finally:
        cleanup()
