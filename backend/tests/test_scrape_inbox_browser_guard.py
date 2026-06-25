"""Regression test: run_inbox_list con 0 contatti -> ready (inbox vuoto legittimo).

Lo scraping inbox via browser e' stato rimosso: ora c'e' un solo engine (API) e
nessun guard 'browser_not_wired'. Una run che esaurisce l'inbox senza contatti
significa che l'account non ha (ancora) thread DM: stato ready valido, non errore.
"""
import asyncio
import os
import tempfile
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.services.scrape_inbox import inbox_collect
from app.services.inbox_source import InboxPage

# Register all ORM tables on Base.metadata (mirrors adversarial test setup)
import app.models.account        # noqa: F401
import app.models.campaign_account  # noqa: F401
import app.models.message        # noqa: F401
import app.models.activity_log   # noqa: F401
import app.models.global_contact # noqa: F401

from app.database import Base
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower
from app.services import scrape_inbox


class _ScriptedSource:
    """Fake InboxListSource that yields a scripted sequence of InboxPages."""

    def __init__(self, pages: list[InboxPage]):
        self._pages = list(pages)
        self._idx = 0

    async def next_page(self) -> InboxPage:
        if self._idx >= len(self._pages):
            return InboxPage(participants=[], cursor=None, exhausted=True)
        page = self._pages[self._idx]
        self._idx += 1
        return page


def _setup_inbox_db(monkeypatch, pages: list[InboxPage], *, inbox_engine: str = "api"):
    """Create a throw-away SQLite DB with one campaign in listing state.

    Patches scrape_inbox.build_inbox_source to return a _ScriptedSource.
    inbox_engine controls which engine the campaign advertises.
    Returns (session_factory, campaign_id, cleanup_fn).
    """
    fd, path = tempfile.mkstemp(suffix=".db", prefix="inbox_bguard_")
    os.close(fd)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    campaign_id = str(uuid.uuid4())

    scripted_source = _ScriptedSource(pages)

    async def _seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_factory() as db:
            db.add(Campaign(
                id=campaign_id,
                name="browser guard test",
                source_type="scrape",
                scrape_mode="dm_threads",
                inbox_engine=inbox_engine,
                status=CampaignStatus.listing,
                messaging_enabled=False,
                scrape_session_size=100_000,
                scrape_break_minutes_min=30,
                scrape_break_minutes_max=45,
            ))
            await db.commit()

    asyncio.run(_seed())

    async def _fake_build_inbox_source(db, campaign):
        async def _noop_cleanup():
            return None
        return scripted_source, 999_999, None, _noop_cleanup

    monkeypatch.setattr(scrape_inbox, "build_inbox_source", _fake_build_inbox_source)

    async def _not_halted(db):
        return False

    monkeypatch.setattr(scrape_inbox, "is_halted", _not_halted)
    monkeypatch.setattr("app.utils.events.emit", lambda *a, **k: None)

    original_sleep = asyncio.sleep

    async def _instant_sleep(seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    def cleanup():
        asyncio.run(engine.dispose())
        try:
            os.remove(path)
        except OSError:
            pass
        monkeypatch.setattr(asyncio, "sleep", original_sleep)

    return session_factory, campaign_id, cleanup


def _run_inbox_list(session_factory, campaign_id):
    async def _go():
        async with session_factory() as db:
            campaign = await db.get(Campaign, campaign_id)
            return await scrape_inbox.run_inbox_list(campaign_id, db, campaign)

    return asyncio.run(asyncio.wait_for(_go(), timeout=10))


def _read_campaign(session_factory, campaign_id):
    """Read campaign status and scrape_outcome from DB after the loop."""
    async def _go():
        async with session_factory() as db:
            c = await db.get(Campaign, campaign_id)
            return c.status, c.scrape_outcome

    return asyncio.run(_go())


# ── api engine + 0 contacts → ready (inbox vuoto legittimo) ──────────────────

def test_api_engine_zero_contacts_stays_ready(monkeypatch):
    """Esaurire l'inbox con 0 contatti → status=ready (inbox genuinamente vuoto),
    non errore. scrape_outcome resta None.
    """
    pages = [InboxPage(participants=[], cursor=None, exhausted=True)]
    session_factory, campaign_id, cleanup = _setup_inbox_db(
        monkeypatch, pages, inbox_engine="api"
    )
    try:
        result = _run_inbox_list(session_factory, campaign_id)
        status, outcome = _read_campaign(session_factory, campaign_id)

        assert result is None, f"Expected None (no break scheduled), got {result!r}"
        assert status == CampaignStatus.ready, (
            f"DEFECT: api engine with 0 contacts must land in ready, got {status!r}"
        )
        # scrape_outcome is not set by the normal completion path — must remain None
        assert outcome is None, (
            f"DEFECT: api 0-contact run must not set scrape_outcome, got {outcome!r}"
        )
    finally:
        cleanup()
