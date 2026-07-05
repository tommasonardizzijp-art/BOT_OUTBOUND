"""Regressione: la Fase Bio cede il job ad ARQ PRIMA di job_timeout (3600s) e
riprende, senza un cap hard sulla durata totale.

Bug originale: un'unica sessione bio (scrape_session_size, default 250) con sleep
per-lead in-job poteva durare > job_timeout (~250 x ~18s = ~4700s). ARQ cancellava
il job a 3600s con TimeoutError (CancelledError) prima di raggiungere il defer di
pausa, quindi la sessione non finiva mai pulita.

Fix: micro-yield ogni MICRO_YIELD_EVERY bio (o MICRO_YIELD_MAX_SECONDS) -> defer
brevissimo, status resta 'scraping', il job successivo riprende dai pending. La
pausa lunga anti-block (scrape_session_size -> 30-45 min) resta separata e ancorata
a `done` (persistito) cosi' i micro-yield non la azzerano.

Questi test pilotano il loop reale di scrape_bios con un fetch_and_store_bio finto
che marca il follower bio_scraped (come fa quello reale) e ritorna "done".
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


def _setup(monkeypatch, n_followers, **campaign_kwargs):
    """Wire scrape_bios contro un DB sqlite temporaneo + un fetch che marca
    bio_scraped (come il reale) e ritorna 'done'. Ritorna (factory, cid, cleanup)."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="bioyield_")
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
                name="bio yield test",
                source_type="scrape",
                target_username="target",
                status=CampaignStatus.scraping,
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

    async def _fetch(follower, campaign, db, pool):
        # Mima fetch_and_store_bio: marca bio_scraped e committa PRIMA di tornare
        # "done" (l'ancora `done` della pausa lunga dipende dalla persistenza).
        follower.status = FollowerStatus.bio_scraped
        follower.updated_at = datetime.utcnow()
        await db.commit()
        return "done", "fake_account", None
    monkeypatch.setattr(scrape_bios, "fetch_and_store_bio", _fetch)

    monkeypatch.setattr("app.utils.events.emit", lambda *a, **k: None)

    _real_sleep = asyncio.sleep

    async def _fast_sleep(_):
        await _real_sleep(0)
    monkeypatch.setattr(scrape_bios.asyncio, "sleep", _fast_sleep)
    # delay/minuti deterministici: ritorna il minimo del range.
    monkeypatch.setattr(scrape_bios.random, "uniform", lambda a, b: a)

    def cleanup():
        asyncio.run(engine.dispose())
        try:
            os.remove(path)
        except OSError:
            pass

    return session_factory, campaign_id, cleanup


async def _counts(session_factory, campaign_id):
    async with session_factory() as db:
        scraped = await db.scalar(select(func.count(Follower.id)).where(
            Follower.campaign_id == campaign_id, Follower.status == FollowerStatus.bio_scraped,
        ))
        pending = await db.scalar(select(func.count(Follower.id)).where(
            Follower.campaign_id == campaign_id, Follower.status == FollowerStatus.pending,
        ))
        campaign = await db.get(Campaign, campaign_id)
        return scraped, pending, campaign.status, campaign.scrape_break_until


def test_micro_yield_defers_then_resumes_to_completion(monkeypatch):
    """150 follower, micro-yield a 100: il 1o job cede (defer breve, status resta
    scraping), il 2o job finisce i restanti 50 e completa (ready). Nessun cap hard."""
    monkeypatch.setattr(scrape_bios, "MICRO_YIELD_EVERY", 100)
    session_factory, campaign_id, cleanup = _setup(monkeypatch, 150)
    try:
        async def _go():
            rets = []
            for _ in range(10):  # safety cap: il loop reale e' guidato dal worker
                ret = await asyncio.wait_for(scrape_bios.scrape_bios(campaign_id), timeout=10)
                rets.append(ret)
                if ret is None:
                    break
            return rets

        rets = asyncio.run(_go())
        scraped, pending, status, _ = asyncio.run(_counts(session_factory, campaign_id))

        # 1o job: micro-yield (defer breve, truthy ma piccolo) dopo 100 bio.
        assert rets[0] == 2, f"atteso micro-yield (defer 2) al 1o job, ottenuto {rets[0]}"
        # 2o job: completamento (return None -> il worker non ri-accoda).
        assert rets[-1] is None, f"atteso completamento (None) all'ultimo job, ottenuto {rets[-1]}"
        assert len(rets) == 2, f"attesi 2 job (yield + completamento), ottenuti {len(rets)}: {rets}"
        assert scraped == 150, f"attese 150 bio totali, trovate {scraped}"
        assert pending == 0, f"nessun pending atteso, trovati {pending}"
        assert status == CampaignStatus.ready
    finally:
        cleanup()


def test_long_break_still_fires_and_survives_restart(monkeypatch):
    """La pausa lunga anti-block scatta al cap della mini-sessione (current_session_cap,
    qui 5) con un defer lungo (status scraping_break) e, al rientro, la run riprende e
    completa. Il cap e' persistito -> next_long_break sopravvive al restart del job."""
    # MICRO_YIELD_EVERY alto: non interferisce con la pausa lunga a 5.
    monkeypatch.setattr(scrape_bios, "MICRO_YIELD_EVERY", 10000)
    session_factory, campaign_id, cleanup = _setup(
        monkeypatch, 8,
        current_session_cap=5,  # cap mini-sessione pre-fissato (nuovo meccanismo)
        scrape_break_minutes_min=30,
        scrape_break_minutes_max=45,
    )
    try:
        async def _go():
            rets = []
            for _ in range(10):
                ret = await asyncio.wait_for(scrape_bios.scrape_bios(campaign_id), timeout=10)
                rets.append(ret)
                if ret is None:
                    break
            return rets

        # Stato dopo il 1o job (deve essere la pausa lunga).
        async def _first_only():
            ret = await asyncio.wait_for(scrape_bios.scrape_bios(campaign_id), timeout=10)
            scraped, pending, status, until = await _counts(session_factory, campaign_id)
            return ret, scraped, pending, status, until

        ret1, scraped1, pending1, status1, until1 = asyncio.run(_first_only())
        # minuti = min(30) -> 1800s (random.uniform patchato a -> a).
        assert ret1 == 1800, f"atteso defer pausa lunga 1800s, ottenuto {ret1}"
        assert status1 == CampaignStatus.scraping_break, f"atteso scraping_break, ottenuto {status1}"
        assert scraped1 == 5, f"attese 5 bio prima della pausa, trovate {scraped1}"
        assert pending1 == 3
        assert until1 is not None, "scrape_break_until deve essere impostato sulla pausa lunga"

        # Restart (come il worker dopo il defer): flippa scraping_break->scraping e finisce.
        async def _resume():
            return await asyncio.wait_for(scrape_bios.scrape_bios(campaign_id), timeout=10)
        ret2 = asyncio.run(_resume())
        scraped2, pending2, status2, _ = asyncio.run(_counts(session_factory, campaign_id))
        assert ret2 is None, f"atteso completamento al 2o job, ottenuto {ret2}"
        assert scraped2 == 8, f"attese 8 bio totali, trovate {scraped2}"
        assert pending2 == 0
        assert status2 == CampaignStatus.ready
    finally:
        cleanup()
