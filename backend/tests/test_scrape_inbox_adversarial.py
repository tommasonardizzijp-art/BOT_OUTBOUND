"""Adversarial tests for inbox_collect (Part A) and run_inbox_list loop (Part B).

Part A: hostile inputs at inbox_collect — the dedup-frontier pure function.
Part B: run_inbox_list driven by a scripted fake InboxListSource + SQLite DB.
"""
import asyncio
import os
import tempfile
import time
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.services.scrape_inbox import inbox_collect
from app.services.inbox_source import InboxPage


# ═══════════════════════════════════════════════════════════════════
# PART A — inbox_collect hostile inputs
# ═══════════════════════════════════════════════════════════════════

# ── contract helpers ────────────────────────────────────────────────

def _assert_dedup_frontier(result, existing_ids):
    """Core invariants that every call to inbox_collect must satisfy."""
    pks = [pk for pk, _ in result]
    # 1. no pk already in existing_ids
    in_existing = [pk for pk in pks if pk in existing_ids]
    assert not in_existing, f"PKs still in existing_ids after collect: {in_existing}"
    # 2. no duplicate pk in output
    assert len(pks) == len(set(pks)), f"Duplicate PKs in output: {pks}"
    # 3. output is a list of (int, str) tuples
    for item in result:
        assert isinstance(item, tuple) and len(item) == 2


# ── empty / trivial inputs ──────────────────────────────────────────

def test_empty_participants_empty_existing():
    result = inbox_collect([], set())
    assert result == []


def test_empty_participants_nonempty_existing():
    result = inbox_collect([], {1, 2, 3})
    assert result == []


def test_nonempty_participants_empty_existing():
    participants = [(1, "alice"), (2, "bob")]
    result = inbox_collect(participants, set())
    assert result == [(1, "alice"), (2, "bob")]
    _assert_dedup_frontier(result, set())


# ── duplicates within one page ──────────────────────────────────────

def test_same_pk_twice_in_page():
    """Same pk appears twice on the page — only first occurrence kept."""
    participants = [(10, "alice"), (20, "bob"), (10, "alice_again")]
    result = inbox_collect(participants, set())
    assert result == [(10, "alice"), (20, "bob")]
    _assert_dedup_frontier(result, set())


def test_same_pk_three_times_in_page():
    """Triple duplicate — first occurrence wins, others discarded."""
    participants = [(5, "x"), (5, "y"), (5, "z")]
    result = inbox_collect(participants, set())
    assert result == [(5, "x")]
    _assert_dedup_frontier(result, set())


# ── pk already in existing_ids ──────────────────────────────────────

def test_pk_in_existing_ids_filtered():
    existing = {123, 456}
    participants = [(123, "mario"), (789, "gino"), (456, "lucia")]
    result = inbox_collect(participants, existing)
    assert result == [(789, "gino")]
    _assert_dedup_frontier(result, existing)


def test_all_pks_in_existing_ids():
    existing = {1, 2, 3}
    participants = [(1, "a"), (2, "b"), (3, "c")]
    result = inbox_collect(participants, existing)
    assert result == []
    _assert_dedup_frontier(result, existing)


# ── pk both in existing_ids AND duplicated in-page ──────────────────

def test_pk_in_existing_and_duplicated_in_page():
    """A pk that is already saved AND appears multiple times on the page.

    Must be filtered entirely — first occurrence by existing_ids filter,
    subsequent by intra-page dedup. Output: empty for that pk.
    """
    existing = {42}
    participants = [(42, "first"), (42, "second"), (99, "fresh")]
    result = inbox_collect(participants, existing)
    assert result == [(99, "fresh")]
    _assert_dedup_frontier(result, existing)


def test_pk_in_existing_three_times_and_new_pks():
    existing = {7, 8}
    participants = [
        (7, "seven_a"), (8, "eight_a"),
        (9, "nine"), (7, "seven_b"),
        (10, "ten"), (8, "eight_b"),
        (9, "nine_again"),
    ]
    result = inbox_collect(participants, existing)
    assert result == [(9, "nine"), (10, "ten")]
    _assert_dedup_frontier(result, existing)


# ── very large page ─────────────────────────────────────────────────

def test_large_page_no_hang_preserves_order_and_first_occurrence():
    """10000 participants, many duplicates: must not hang, order preserved, first wins."""
    n = 10_000
    # Every pk appears twice (pk 0..4999 twice each)
    participants = [(i % 5000, f"user_{i}") for i in range(n)]
    start = time.monotonic()
    result = inbox_collect(participants, set())
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"inbox_collect too slow on 10k input: {elapsed:.3f}s"
    # 5000 unique pks, first occurrence (username user_0..user_4999)
    assert len(result) == 5000
    pks = [pk for pk, _ in result]
    assert pks == list(range(5000)), "Order not preserved — first-occurrence order broken"
    # First occurrence: user_0 for pk=0, user_1 for pk=1, ...
    for pk, username in result:
        assert username == f"user_{pk}", f"First occurrence not preserved for pk={pk}: got {username!r}"
    _assert_dedup_frontier(result, set())


def test_large_page_with_large_existing():
    """10000 participants, 10000 in existing_ids — intersection handled correctly."""
    existing_ids = set(range(10_000))
    # All these pks are already known
    participants = [(i, f"u{i}") for i in range(10_000)]
    result = inbox_collect(participants, existing_ids)
    assert result == []


def test_large_page_partial_overlap():
    """10000 participants: first 5000 known, last 5000 fresh (with intra-page dupes)."""
    existing_ids = set(range(5_000))
    participants = [(i, f"u{i}") for i in range(10_000)]
    result = inbox_collect(participants, existing_ids)
    assert len(result) == 5_000
    pks = [pk for pk, _ in result]
    assert pks == list(range(5_000, 10_000))
    _assert_dedup_frontier(result, existing_ids)


# ── same pk, different usernames (first wins) ────────────────────────

def test_same_pk_different_usernames_first_wins():
    """Same pk with different usernames on the same page — first occurrence preserved."""
    participants = [
        (100, "original_name"),
        (200, "other"),
        (100, "renamed"),   # same pk, different username
        (100, "third"),
    ]
    result = inbox_collect(participants, set())
    assert result == [(100, "original_name"), (200, "other")]
    _assert_dedup_frontier(result, set())


def test_pk_in_existing_but_also_different_username_in_page():
    """pk in existing_ids + different username in page → fully discarded."""
    existing = {77}
    participants = [(77, "old_name"), (77, "new_name"), (88, "fresh")]
    result = inbox_collect(participants, existing)
    assert result == [(88, "fresh")]
    _assert_dedup_frontier(result, existing)


# ── large existing_ids set ──────────────────────────────────────────

def test_large_existing_ids_correctness():
    """existing_ids = 10000 elements, all page pks in existing: should return []."""
    existing_ids = set(range(10_000))
    participants = [(i, f"u{i}") for i in range(100)]  # subset of existing
    result = inbox_collect(participants, existing_ids)
    assert result == []
    _assert_dedup_frontier(result, existing_ids)


def test_large_existing_ids_with_fresh_pks():
    """existing_ids = 10000, page has some pks outside that range."""
    existing_ids = set(range(10_000))
    participants = [(9999, "last_known"), (10_000, "first_fresh"), (10_001, "second_fresh")]
    result = inbox_collect(participants, existing_ids)
    assert result == [(10_000, "first_fresh"), (10_001, "second_fresh")]
    _assert_dedup_frontier(result, existing_ids)


# ── order preservation edge cases ───────────────────────────────────

def test_order_preserved_with_mixed_existing_and_dupes():
    """Output order follows first-occurrence order, even with interleaved filtering."""
    existing = {10, 30}
    participants = [
        (10, "skip_existing"),
        (20, "keep_first"),
        (30, "skip_existing2"),
        (20, "skip_dupe"),
        (40, "keep_second"),
        (50, "keep_third"),
        (40, "skip_dupe2"),
    ]
    result = inbox_collect(participants, existing)
    assert result == [(20, "keep_first"), (40, "keep_second"), (50, "keep_third")]
    _assert_dedup_frontier(result, existing)


# ═══════════════════════════════════════════════════════════════════
# PART B — run_inbox_list driven by a fake InboxListSource + SQLite
# ═══════════════════════════════════════════════════════════════════

# Import models lazily inside tests to avoid import-time issues
# (all other tests in this module are pure; DB imports only for Part B)

import app.models.account        # noqa: F401 — register in metadata
import app.models.campaign_account  # noqa: F401
import app.models.message        # noqa: F401
import app.models.activity_log   # noqa: F401
import app.models.global_contact # noqa: F401

from app.database import Base
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.services import scrape_inbox


class _ScriptedSource:
    """Fake InboxListSource that yields a scripted sequence of InboxPages."""

    def __init__(self, pages: list[InboxPage]):
        self._pages = list(pages)
        self._idx = 0

    async def next_page(self) -> InboxPage:
        if self._idx >= len(self._pages):
            # Safety: never called past last page (loop should have stopped)
            return InboxPage(participants=[], cursor=None, exhausted=True)
        page = self._pages[self._idx]
        self._idx += 1
        return page


def _setup_inbox_db(monkeypatch, pages: list[InboxPage], *, list_target=None):
    """Create a throw-away SQLite DB with one campaign in listing state.

    Patches scrape_inbox.build_inbox_source to return a _ScriptedSource.
    Returns (session_factory, campaign_id, cleanup_fn).
    """
    from app.database import Base

    fd, path = tempfile.mkstemp(suffix=".db", prefix="inbox_adv_")
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
                name="inbox adversarial test",
                source_type="scrape",
                scrape_mode="dm_threads",
                inbox_engine="api",
                status=CampaignStatus.listing,
                messaging_enabled=False,
                list_target=list_target,
                scrape_session_size=100_000,  # no session break in tests
                scrape_break_minutes_min=30,
                scrape_break_minutes_max=45,
            ))
            await db.commit()

    asyncio.run(_seed())

    # Patch build_inbox_source to return our scripted fake
    async def _fake_build_inbox_source(db, campaign):
        async def _noop_cleanup():
            return None
        own_pk = 999_999
        return scripted_source, own_pk, None, _noop_cleanup

    monkeypatch.setattr(scrape_inbox, "build_inbox_source", _fake_build_inbox_source)

    # Patch is_halted to always return False (bot not halted)
    async def _not_halted(db):
        return False
    monkeypatch.setattr(scrape_inbox, "is_halted", _not_halted)

    # Silence event emitter
    monkeypatch.setattr("app.utils.events.emit", lambda *a, **k: None)

    # Silence API page delay (would call asyncio.sleep in api engine path)
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
    """Run run_inbox_list inside a real async DB session, return result."""
    async def _go():
        async with session_factory() as db:
            campaign = await db.get(Campaign, campaign_id)
            return await scrape_inbox.run_inbox_list(campaign_id, db, campaign)

    return asyncio.run(asyncio.wait_for(_go(), timeout=10))


def _read_state(session_factory, campaign_id):
    """Read follower count and campaign status from DB after the loop."""
    async def _go():
        async with session_factory() as db:
            cnt = await db.scalar(
                select(func.count(Follower.id)).where(Follower.campaign_id == campaign_id)
            )
            c = await db.get(Campaign, campaign_id)
            return cnt, c.status, c.total_followers

    return asyncio.run(_go())


# ── Part B test 1: immediately exhausted source ──────────────────────

def test_source_exhausted_immediately(monkeypatch):
    """Source returns exhausted=True on first page with no participants.

    Expected: loop completes, campaign status = ready, no followers added, no crash.
    """
    pages = [InboxPage(participants=[], cursor=None, exhausted=True)]
    session_factory, campaign_id, cleanup = _setup_inbox_db(monkeypatch, pages)
    try:
        result = _run_inbox_list(session_factory, campaign_id)
        cnt, status, total = _read_state(session_factory, campaign_id)
        assert result is None, f"Expected None (completed), got {result!r}"
        assert status == CampaignStatus.ready, f"Expected ready, got {status!r}"
        assert cnt == 0
        assert total == 0
    finally:
        cleanup()


# ── Part B test 2: all-duplicate participants, then exhausted ────────

def test_all_duplicate_participants_no_infinite_growth(monkeypatch):
    """Source returns only duplicate pks on every page (all already-saved after page 1),
    then exhausted=True. Must not grow infinitely, must terminate correctly.
    """
    # Page 1: 3 fresh participants (will be saved)
    # Page 2: same 3 pks again (all already saved → dedup filters to 0)
    # Page 3: exhausted
    pages = [
        InboxPage(participants=[(1, "a"), (2, "b"), (3, "c")], cursor="c1", exhausted=False),
        InboxPage(participants=[(1, "a"), (2, "b"), (3, "c")], cursor="c2", exhausted=False),
        InboxPage(participants=[], cursor=None, exhausted=True),
    ]
    session_factory, campaign_id, cleanup = _setup_inbox_db(monkeypatch, pages)
    try:
        result = _run_inbox_list(session_factory, campaign_id)
        cnt, status, total = _read_state(session_factory, campaign_id)
        assert result is None
        assert status == CampaignStatus.ready
        # Page 1 saves 3 fresh; page 2 saves 0 (all deduped); total = 3
        assert cnt == 3, f"Expected 3 followers (page 1 only), got {cnt}"
        assert total == 3
    finally:
        cleanup()


# ── Part B test 3: yields fresh participants then exhausted ──────────

def test_yields_fresh_then_exhausted(monkeypatch):
    """Source yields fresh participants over multiple pages then exhausts.
    Correct count must be persisted.
    """
    pages = [
        InboxPage(participants=[(10, "u10"), (11, "u11"), (12, "u12")], cursor="c1", exhausted=False),
        InboxPage(participants=[(13, "u13"), (14, "u14")], cursor="c2", exhausted=False),
        InboxPage(participants=[(15, "u15")], cursor=None, exhausted=True),
    ]
    session_factory, campaign_id, cleanup = _setup_inbox_db(monkeypatch, pages)
    try:
        result = _run_inbox_list(session_factory, campaign_id)
        cnt, status, total = _read_state(session_factory, campaign_id)
        assert result is None
        assert status == CampaignStatus.ready
        assert cnt == 6, f"Expected 6 followers across 3 pages, got {cnt}"
        assert total == 6
    finally:
        cleanup()


# ── Part B test 4: list_target stop before exhaustion ───────────────

def test_list_target_reached_stops_loop(monkeypatch):
    """If list_target is reached, loop stops before the source is exhausted.

    The source has many pages, but list_target=3 means the loop stops after
    the first page that brings total to >= 3.
    """
    pages = [
        InboxPage(participants=[(20, "u20"), (21, "u21"), (22, "u22")], cursor="c1", exhausted=False),
        # This page should never be fetched because list_target=3 is already hit
        InboxPage(participants=[(23, "u23")], cursor=None, exhausted=True),
    ]
    session_factory, campaign_id, cleanup = _setup_inbox_db(monkeypatch, pages, list_target=3)
    try:
        result = _run_inbox_list(session_factory, campaign_id)
        cnt, status, total = _read_state(session_factory, campaign_id)
        assert result is None
        assert status == CampaignStatus.ready
        assert cnt == 3
        assert total == 3
    finally:
        cleanup()


# ── Part B: drain-stop su pagine consecutive senza nuovi ─────────────

class _CountingSource:
    """Sorgente inbox controllata da una funzione page(n). Conta le chiamate.

    Cruciale: genera pagine NON esaurite (exhausted=False, has_older sempre True)
    all'infinito -> senza il drain-stop il loop girerebbe per sempre (il bug reale:
    IG tiene has_older=True in coda). Il test lo prova: se il drain-stop non scatta,
    _run_inbox_list va in timeout (wait_for=10s) e fallisce.
    """

    def __init__(self, page_fn):
        self.calls = 0
        self._fn = page_fn

    async def next_page(self) -> InboxPage:
        self.calls += 1
        return self._fn(self.calls)


def _inject_source(monkeypatch, src):
    async def _fake_build(db, campaign):
        async def _noop():
            return None
        return src, 999_999, None, _noop
    from app.services import scrape_inbox
    monkeypatch.setattr(scrape_inbox, "build_inbox_source", _fake_build)


def test_drain_stop_ferma_loop_infinito_di_duplicati(monkeypatch):
    """Pagine vuote/non-esaurite all'infinito: il loop si ferma dopo esattamente
    inbox_empty_page_stop pagine (non gira a vuoto per sempre) e va in ready."""
    from app.config import settings
    session_factory, campaign_id, cleanup = _setup_inbox_db(monkeypatch, [])
    try:
        src = _CountingSource(lambda n: InboxPage(participants=[], cursor=f"c{n}", exhausted=False))
        _inject_source(monkeypatch, src)
        result = _run_inbox_list(session_factory, campaign_id)
        cnt, status, total = _read_state(session_factory, campaign_id)
        assert result is None
        assert status == CampaignStatus.ready
        assert cnt == 0
        assert src.calls == settings.inbox_empty_page_stop, (
            f"atteso stop dopo {settings.inbox_empty_page_stop} pagine, fermato a {src.calls}"
        )
    finally:
        cleanup()


def test_un_nuovo_contatto_resetta_lo_streak(monkeypatch):
    """Uno streak di vuoti interrotto da 1 nuovo NON deve fermare: lo streak riparte
    da zero, e il drain-stop scatta solo dopo inbox_empty_page_stop CONSECUTIVI."""
    from app.config import settings

    def page_fn(n):
        # pagina 6 porta 1 nuovo, tutte le altre sono vuote
        if n == 6:
            return InboxPage(participants=[(100, "nuovo")], cursor=f"c{n}", exhausted=False)
        return InboxPage(participants=[], cursor=f"c{n}", exhausted=False)

    session_factory, campaign_id, cleanup = _setup_inbox_db(monkeypatch, [])
    try:
        src = _CountingSource(page_fn)
        _inject_source(monkeypatch, src)
        result = _run_inbox_list(session_factory, campaign_id)
        cnt, status, total = _read_state(session_factory, campaign_id)
        assert result is None
        assert status == CampaignStatus.ready
        assert cnt == 1  # l'unico nuovo e' stato salvato
        # streak: pagine 1-5 vuote, 6 nuovo (reset), poi 8 vuote consecutive -> stop a 6+8
        assert src.calls == 6 + settings.inbox_empty_page_stop, f"fermato a {src.calls}"
    finally:
        cleanup()
