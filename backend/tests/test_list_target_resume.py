"""Regressione: alzare il list_target dopo una Fase Lista 'completata' deve poter
raccogliere piu' follower, e il cursore IG non va perso quando lo stop e' per target
raggiunto (solo l'esaurimento reale della lista azzera il cursore).

Bug originale (campagna 'Scraping Shop survivor x AV', list_target=1900, cursor=None):
  1. start_list applicava body.target DOPO il guard rescan -> il nuovo target (4000)
     non veniva mai salvato (HTTP 400 prima).
  2. scrape_list azzerava scrape_cursor anche quando lo stop era 'target raggiunto'
     -> impossibile riprendere dalla posizione IG.
  3. _fetch_followers_chunk mascherava un throttle mid-paginazione come fine lista
     (fallback ritorna max_id=None).

Copre i 3 punti: helper guard, loop list_followers (cursore conservato vs azzerato),
e il re-raise di _fetch_followers_chunk.
"""
import asyncio
import os
import tempfile
import uuid

import pytest
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

from app.api.campaigns import list_start_blocked
from app.services import scrape_list


# ───────────────────────── 1) guard puro ─────────────────────────

def test_guard_allows_resume_when_cursor_present():
    # cursore presente => si puo' sempre riprendere
    assert list_start_blocked("max_id_123", existing_count=1900, list_target=1900) is False


def test_guard_allows_first_run():
    assert list_start_blocked(None, existing_count=0, list_target=4000) is False


def test_guard_blocks_target_reached():
    # cursore perso + gia' raccolti >= target => satura
    assert list_start_blocked(None, existing_count=1900, list_target=1900) is True


def test_guard_allows_when_target_raised():
    # IL BUG DELL'UTENTE: 1900 in DB, target alzato a 4000 => deve permettere
    assert list_start_blocked(None, existing_count=1900, list_target=4000) is False


def test_guard_blocks_whole_list_drained():
    # target None (lista intera) gia' drenata => satura
    assert list_start_blocked(None, existing_count=1127, list_target=None) is True


# ───────────────────── 2) loop list_followers ─────────────────────

class _FakeUser:
    def __init__(self, pk: int):
        self.pk = str(pk)
        self.username = f"u{pk}"
        self.full_name = f"User {pk}"
        self.is_private = False
        self.is_verified = False
        self.profile_pic_url = None


class _FakeAccount:
    username = "fake_scraper"


class _FakePool:
    @classmethod
    async def build(cls, db, campaign):
        return cls()

    def next(self, campaign):
        return (_FakeAccount(), "fake_client")

    async def save_sessions(self, db):
        pass

    async def release(self):
        pass


def _setup(monkeypatch, fetch_fn, *, list_target, existing_cursor=None):
    fd, path = tempfile.mkstemp(suffix=".db", prefix="listtgt_")
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
                name="list target test",
                source_type="scrape",
                target_username="target",
                target_user_id=12345,
                status=CampaignStatus.listing,
                messaging_enabled=False,
                list_target=list_target,
                scrape_session_size=10_000,  # niente session break nel test
                scrape_cursor=existing_cursor,
            ))
            await db.commit()

    asyncio.run(_seed())

    monkeypatch.setattr(scrape_list, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(scrape_list, "ScrapingPool", _FakePool)
    monkeypatch.setattr(scrape_list, "_fetch_followers_chunk", fetch_fn)

    async def _not_halted(db):
        return False
    monkeypatch.setattr(scrape_list, "is_halted", _not_halted)

    async def _no_delay():
        return None
    monkeypatch.setattr(scrape_list, "_list_page_delay", _no_delay)
    monkeypatch.setattr("app.utils.events.emit", lambda *a, **k: None)

    def cleanup():
        asyncio.run(engine.dispose())
        try:
            os.remove(path)
        except OSError:
            pass

    return session_factory, campaign_id, cleanup


def test_target_reached_preserves_cursor(monkeypatch):
    """Stop per target raggiunto: cursore CONSERVATO (riprendibile), status ready."""
    counter = {"pk": 0}

    def fake_fetch(client, user_id, amount, max_id, scrape_mode):
        start = counter["pk"]
        users = [_FakeUser(1000 + start + i) for i in range(amount)]
        counter["pk"] = start + amount
        return users, f"cursor_{counter['pk']}"  # IG ha sempre altro (mai vuoto)

    session_factory, campaign_id, cleanup = _setup(monkeypatch, fake_fetch, list_target=5)
    try:
        async def _go():
            await asyncio.wait_for(scrape_list.list_followers(campaign_id), timeout=10)
            async with session_factory() as db:
                cnt = await db.scalar(select(func.count(Follower.id)).where(Follower.campaign_id == campaign_id))
                c = await db.get(Campaign, campaign_id)
                return cnt, c.status, c.scrape_cursor

        cnt, status, cursor = asyncio.run(_go())
        assert cnt == 5, f"attesi 5 follower (target), trovati {cnt}"
        assert status == CampaignStatus.ready
        assert cursor is not None, "cursore NON deve essere azzerato su stop=target raggiunto"
    finally:
        cleanup()


def test_ig_exhausted_wipes_cursor(monkeypatch):
    """Stop per lista IG esaurita (batch vuoto): cursore AZZERATO, status ready."""
    calls = {"n": 0}

    def fake_fetch(client, user_id, amount, max_id, scrape_mode):
        calls["n"] += 1
        if calls["n"] == 1:
            return [_FakeUser(2000 + i) for i in range(3)], "cursor_after_1"
        return [], None  # IG esaurita

    session_factory, campaign_id, cleanup = _setup(monkeypatch, fake_fetch, list_target=None)
    try:
        async def _go():
            await asyncio.wait_for(scrape_list.list_followers(campaign_id), timeout=10)
            async with session_factory() as db:
                cnt = await db.scalar(select(func.count(Follower.id)).where(Follower.campaign_id == campaign_id))
                c = await db.get(Campaign, campaign_id)
                return cnt, c.status, c.scrape_cursor

        cnt, status, cursor = asyncio.run(_go())
        assert cnt == 3
        assert status == CampaignStatus.ready
        assert cursor is None, "cursore deve essere azzerato quando IG ha davvero finito"
    finally:
        cleanup()


# ───────────────── 3) _fetch_followers_chunk re-raise ─────────────────

class _ThrottleClient:
    """Il chunk fallisce sempre; il fallback non-chunk avrebbe successo."""
    def user_followers_v1_chunk(self, user_id, max_amount, max_id):
        raise Exception("429 Too Many Requests")

    def user_followers(self, user_id, amount):
        return {f"{i}": _FakeUser(i) for i in range(amount)}

    user_following_v1_chunk = user_followers_v1_chunk

    def user_following(self, user_id, amount):
        return {f"{i}": _FakeUser(i) for i in range(amount)}


def test_chunk_reraises_mid_pagination():
    """max_id presente (mid-paginazione): un errore NON va mascherato come fine lista."""
    from app.services.scraper import _fetch_followers_chunk
    with pytest.raises(Exception, match="429"):
        _fetch_followers_chunk(_ThrottleClient(), 1, 30, "some_cursor", "followers")
    with pytest.raises(Exception, match="429"):
        _fetch_followers_chunk(_ThrottleClient(), 1, 30, "some_cursor", "following")


def test_chunk_fallback_only_first_page():
    """max_id assente (prima pagina): fallback non-chunk consentito, cursor=None."""
    from app.services.scraper import _fetch_followers_chunk
    users, cursor = _fetch_followers_chunk(_ThrottleClient(), 1, 4, None, "followers")
    assert len(users) == 4
    assert cursor is None


class _OkClient:
    def user_followers_v1_chunk(self, user_id, max_amount, max_id):
        return [_FakeUser(i) for i in range(max_amount)], "next_cursor"


def test_chunk_success_passes_cursor():
    from app.services.scraper import _fetch_followers_chunk
    users, cursor = _fetch_followers_chunk(_OkClient(), 1, 3, "c0", "followers")
    assert len(users) == 3
    assert cursor == "next_cursor"
