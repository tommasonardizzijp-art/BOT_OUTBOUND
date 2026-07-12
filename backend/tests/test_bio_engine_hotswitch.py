"""Regressione: switch bio_engine api->browser A CALDO mentre un job API gira.

Bug osservato (12/07): l'operatore lancia la Fase Bio, si accorge che parte via
API, la ferma, cambia motore a 'browser' e la riavvia — ma riparte di nuovo via
API. Causa: il job API di scrape_bios legge bio_engine SOLO all'ingresso (dispatch
riga ~90); dentro il while-loop non lo rilegge mai. Finche' quel job esegue tiene
il lock arq `in-progress:bios:{cid}`, che (a) fa saltare l'enqueue del nuovo job
browser da `bios/start` e (b) il job stesso continua a scrapare via API ignorando
lo switch. I defer di pausa (Retry) si auto-sanano perche' rientrano nel dispatch;
un job ATTIVO nel loop no.

Fix: nel loop, dopo il `db.refresh(campaign)`, se bio_engine e' passato a 'browser'
il job API si auto-defer (return ENGINE_SWITCH_DEFER) — arq rilascia in-progress e
ri-accoda bios:{cid}, che al rientro dispaccia sul motore browser.

Pilota il loop reale con un fetch finto (come test_bio_micro_yield) che, alla prima
bio, simula lo switch scrivendo bio_engine='browser'.
"""
import asyncio
import os
import tempfile
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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


def _setup(monkeypatch, n_followers, on_fetch=None, **campaign_kwargs):
    fd, path = tempfile.mkstemp(suffix=".db", prefix="biohotswitch_")
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
                name="bio hotswitch test",
                source_type="scrape",
                target_username="target",
                status=CampaignStatus.scraping,
                bio_engine="api",
                messaging_enabled=False,
                **campaign_kwargs,
            ))
            for i in range(n_followers):
                db.add(Follower(
                    id=str(uuid.uuid4()),
                    campaign_id=campaign_id,
                    ig_user_id=900000000 + i,
                    username=f"user_{i}",
                    status=FollowerStatus.pending,
                ))
            await db.commit()

    asyncio.run(_seed())

    monkeypatch.setattr(scrape_bios, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(scrape_bios, "ScrapingPool", _FakePool)

    async def _not_halted(db):
        return False
    monkeypatch.setattr(scrape_bios, "is_halted", _not_halted)

    calls = {"n": 0}

    async def _fetch(follower, campaign, db, pool):
        calls["n"] += 1
        follower.status = FollowerStatus.bio_scraped
        follower.updated_at = datetime.utcnow()
        if on_fetch is not None:
            await on_fetch(calls["n"], campaign, db)
        await db.commit()
        return "done", "fake_account", None
    monkeypatch.setattr(scrape_bios, "fetch_and_store_bio", _fetch)

    monkeypatch.setattr("app.utils.events.emit", lambda *a, **k: None)

    _real_sleep = asyncio.sleep

    async def _fast_sleep(_):
        await _real_sleep(0)
    monkeypatch.setattr(scrape_bios.asyncio, "sleep", _fast_sleep)
    monkeypatch.setattr(scrape_bios.random, "uniform", lambda a, b: a)

    def cleanup():
        asyncio.run(engine.dispose())
        try:
            os.remove(path)
        except OSError:
            pass

    return session_factory, campaign_id, calls, cleanup


async def _counts(session_factory, campaign_id):
    async with session_factory() as db:
        scraped = await db.scalar(select(func.count(Follower.id)).where(
            Follower.campaign_id == campaign_id, Follower.status == FollowerStatus.bio_scraped,
        ))
        pending = await db.scalar(select(func.count(Follower.id)).where(
            Follower.campaign_id == campaign_id, Follower.status == FollowerStatus.pending,
        ))
        campaign = await db.get(Campaign, campaign_id)
        return scraped, pending, campaign.status


def test_api_job_defers_when_engine_switched_to_browser_midrun(monkeypatch):
    """Dopo la 1a bio l'operatore switcha a browser: il job API deve auto-deferrare
    (return ENGINE_SWITCH_DEFER), NON continuare a scrapare i restanti via API."""
    # micro-yield alto: non deve interferire con la diagnosi.
    monkeypatch.setattr(scrape_bios, "MICRO_YIELD_EVERY", 10000)

    async def _switch_after_first(n, campaign, db):
        if n == 1:
            campaign.bio_engine = "browser"  # committato insieme alla bio nel _fetch

    session_factory, campaign_id, calls, cleanup = _setup(
        monkeypatch, 5, on_fetch=_switch_after_first,
    )
    try:
        ret = asyncio.run(asyncio.wait_for(scrape_bios.scrape_bios(campaign_id), timeout=10))
        scraped, pending, status = asyncio.run(_counts(session_factory, campaign_id))

        assert ret == scrape_bios.ENGINE_SWITCH_DEFER, (
            f"atteso auto-defer {scrape_bios.ENGINE_SWITCH_DEFER} allo switch a browser, ottenuto {ret!r}"
        )
        # Solo la 1a bio (pre-switch) via API; il resto NON deve essere scrapato via API.
        assert calls["n"] == 1, f"il job API ha continuato dopo lo switch: {calls['n']} fetch"
        assert scraped == 1, f"attesa 1 bio pre-switch, trovate {scraped}"
        assert pending == 4, f"i restanti 4 pending non devono essere toccati via API, trovati {pending}"
        # status resta attivo (scraping): il re-dispatch via defer partira' su browser.
        assert status == CampaignStatus.scraping
    finally:
        cleanup()


def test_api_job_runs_normally_when_engine_unchanged(monkeypatch):
    """Controllo negativo: senza switch, il job API completa normalmente (return None)."""
    monkeypatch.setattr(scrape_bios, "MICRO_YIELD_EVERY", 10000)
    session_factory, campaign_id, calls, cleanup = _setup(monkeypatch, 3)
    try:
        ret = asyncio.run(asyncio.wait_for(scrape_bios.scrape_bios(campaign_id), timeout=10))
        scraped, pending, status = asyncio.run(_counts(session_factory, campaign_id))
        assert ret is None, f"atteso completamento (None), ottenuto {ret!r}"
        assert calls["n"] == 3
        assert scraped == 3 and pending == 0
        assert status == CampaignStatus.ready
    finally:
        cleanup()
