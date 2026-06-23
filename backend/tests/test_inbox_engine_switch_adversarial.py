"""
Adversarial tests for the inbox-engine switch in update_campaign.

Part A — pure helper: engine_switch_resets_cursor edge cases.
Part B — endpoint state-gating via TestClient + SQLite fixture.

Design notes:
- Part A needs no DB: tests the pure helper directly.
- Part B mirrors the pattern in test_inbox_guard_adversarial.py (module-scoped
  temp SQLite, dependency_overrides for get_db + get_current_user).
- The endpoint is PUT /{id}, not PATCH. inbox_engine schema validates
  pattern='^(browser|api)$', so odd values ('API', '', None) are rejected
  by Pydantic (422) before the endpoint logic — documented in Part A comments.
"""

import asyncio
import os
import tempfile
import uuid
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.campaigns import engine_switch_resets_cursor

# Register all ORM tables on Base.metadata.
import app.models.account  # noqa: F401
import app.models.activity_log  # noqa: F401
import app.models.campaign_account  # noqa: F401
import app.models.follower  # noqa: F401
import app.models.global_contact  # noqa: F401
import app.models.imported_profile  # noqa: F401
import app.models.message  # noqa: F401
import app.models.user  # noqa: F401

from app.database import Base, get_db
from app.models.campaign import Campaign, CampaignStatus
from app.models.user import User
from app.utils.auth_deps import get_current_user


# ============================================================================
# PART A — pure helper: engine_switch_resets_cursor
# ============================================================================

# --- Equal engines: no switch → cursor must be preserved ---

def test_same_engine_browser_browser_no_reset():
    """Identical engine (browser/browser) → False: cursor survives."""
    assert engine_switch_resets_cursor("browser", "browser") is False


def test_same_engine_api_api_no_reset():
    """Identical engine (api/api) → False: cursor survives."""
    assert engine_switch_resets_cursor("api", "api") is False


# --- Different engines: switch → cursor must be reset ---

def test_switch_browser_to_api_resets_cursor():
    """browser → api: different engines → True, cursor invalidated."""
    assert engine_switch_resets_cursor("browser", "api") is True


def test_switch_api_to_browser_resets_cursor():
    """api → browser: different engines → True, cursor invalidated."""
    assert engine_switch_resets_cursor("api", "browser") is True


# --- Odd / dirty inputs: document that any difference forces reset ---
#
# NOTE: in the real endpoint, inbox_engine on CampaignUpdate is validated with
# pattern='^(browser|api)$', so 'API', '', and None are rejected by Pydantic
# (→ 422) before the function is ever called. The tests below verify the
# *helper's own behavior* with such values so we understand whether the helper
# itself has a safety net.
#
# In every case where old != new, the helper returns True (safe direction:
# cursor gets reset). Returning True on a dirty value is harmless — it just
# resets the cursor when in doubt, which prevents using a stale token from
# a different engine context.

def test_none_old_vs_api_new_returns_true():
    """
    DB field defaults to 'browser', but if somehow old_engine is None
    (pre-migration row) and new_engine is 'api', they differ → True.
    Safe: cursor is wiped rather than risked as a cross-engine token.
    """
    assert engine_switch_resets_cursor(None, "api") is True


def test_none_old_vs_browser_new_returns_true():
    """None old vs 'browser' new → True (any difference → reset)."""
    assert engine_switch_resets_cursor(None, "browser") is True


def test_none_vs_none_returns_false():
    """
    Both None: no difference → False. A pair of pre-migration rows that
    are both None won't spuriously invalidate each other's cursor.
    """
    assert engine_switch_resets_cursor(None, None) is False


def test_uppercase_API_vs_api_returns_true():
    """
    'API' vs 'api': case mismatch → True (safe direction).
    The Pydantic schema on CampaignUpdate rejects 'API' (422), so
    this combination cannot reach the endpoint via normal flow. But if
    the DB somehow stores 'API' (e.g. a direct write), the helper treats it
    as a different engine and wipes the cursor — correct behaviour.
    """
    assert engine_switch_resets_cursor("API", "api") is True


def test_uppercase_BROWSER_vs_browser_returns_true():
    """'BROWSER' vs 'browser': case mismatch → True (safe)."""
    assert engine_switch_resets_cursor("BROWSER", "browser") is True


def test_uppercase_same_returns_false():
    """
    'API' vs 'API': identical dirty values → False.
    No switch, no reset — consistent with the equal-engine contract.
    """
    assert engine_switch_resets_cursor("API", "API") is False


def test_empty_string_old_vs_browser_returns_true():
    """
    '' vs 'browser': they differ → True.
    An empty DB value is not a valid engine; treating it as different is safe.
    """
    assert engine_switch_resets_cursor("", "browser") is True


def test_empty_string_old_vs_api_returns_true():
    """'' vs 'api' → True (safe direction)."""
    assert engine_switch_resets_cursor("", "api") is True


def test_empty_string_both_returns_false():
    """'' vs '' → False: equal, no reset."""
    assert engine_switch_resets_cursor("", "") is False


def test_whitespace_variants_return_true():
    """
    ' browser' (leading space) vs 'browser': differ → True.
    Any whitespace-polluted DB value forces a cursor reset.
    """
    assert engine_switch_resets_cursor(" browser", "browser") is True
    assert engine_switch_resets_cursor("browser ", "browser") is True
    assert engine_switch_resets_cursor(" api", "api") is True


# ============================================================================
# PART B — endpoint state-gating
# ============================================================================

# ---------- Fixtures (module-scoped temp SQLite, mirrors guard adversarial) --

@pytest.fixture(scope="module")
def _temp_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="e2e_engine_switch_")
    os.close(fd)
    url = f"sqlite+aiosqlite:///{path}"
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    yield engine, session_factory

    async def _dispose():
        await engine.dispose()

    asyncio.run(_dispose())
    try:
        os.remove(path)
    except OSError:
        pass


@pytest.fixture(scope="module")
def client(_temp_db):
    engine, session_factory = _temp_db

    async def _override_get_db():
        async with session_factory() as session:
            try:
                yield session
            finally:
                await session.close()

    def _override_get_current_user():
        return User(
            id="00000000-0000-0000-0000-000000000002",
            email="admin@test.local",
            password_hash="x",
            role="admin",
            is_active=True,
            created_at=datetime.utcnow(),
        )

    from app.main import app

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _override_get_current_user

    from fastapi.testclient import TestClient

    c = TestClient(app, raise_server_exceptions=True)
    yield c

    app.dependency_overrides.clear()


def _run(session_factory, coro_fn):
    """Run an async DB helper synchronously (mirrors existing helpers)."""
    async def _wrap():
        async with session_factory() as db:
            return await coro_fn(db)
    return asyncio.run(_wrap())


def _make_campaign(
    *,
    name: str,
    status: CampaignStatus,
    scrape_cursor: str | None = None,
    inbox_engine: str = "browser",
    scrape_mode: str = "dm_threads",
    messaging_enabled: bool = False,
) -> Campaign:
    return Campaign(
        id=str(uuid.uuid4()),
        name=name,
        source_type="scrape",
        target_username="target_user",
        scrape_mode=scrape_mode,
        inbox_engine=inbox_engine,
        scrape_cursor=scrape_cursor,
        status=status,
        messaging_enabled=messaging_enabled,
    )


# ---------- B-1: paused campaign, engine switch → cursor reset ---------------

def test_engine_switch_on_paused_resets_cursor(client, _temp_db):
    """
    dm_threads, paused, scrape_cursor='ABC', inbox_engine='browser'.
    PUT inbox_engine='api' → 200, scrape_cursor becomes None, inbox_engine=='api'.
    """
    _, sf = _temp_db
    camp = _make_campaign(
        name="B1-paused-switch",
        status=CampaignStatus.paused,
        scrape_cursor="ABC",
        inbox_engine="browser",
    )
    camp_id = camp.id

    async def _seed(db):
        db.add(camp)
        await db.commit()

    _run(sf, _seed)

    resp = client.put(
        f"/api/campaigns/{camp_id}",
        json={"inbox_engine": "api"},
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["inbox_engine"] == "api", f"inbox_engine not updated: {body['inbox_engine']}"
    assert body["scrape_cursor"] is None, (
        f"DEFECT: cursor should be reset to None on engine switch, got: {body['scrape_cursor']}"
    )


# ---------- B-2: same engine → cursor must NOT be reset ---------------------

def test_same_engine_patch_preserves_cursor(client, _temp_db):
    """
    dm_threads, paused, scrape_cursor='ABC', inbox_engine='browser'.
    PUT inbox_engine='browser' (no change) → 200, scrape_cursor still 'ABC'.

    This is the key idempotency contract: re-setting the same engine must not
    destroy progress. If cursor is reset here, a UI that always sends the full
    update payload would silently lose the scraping position.
    """
    _, sf = _temp_db
    camp = _make_campaign(
        name="B2-same-engine-keeps-cursor",
        status=CampaignStatus.paused,
        scrape_cursor="ABC",
        inbox_engine="browser",
    )
    camp_id = camp.id

    async def _seed(db):
        db.add(camp)
        await db.commit()

    _run(sf, _seed)

    resp = client.put(
        f"/api/campaigns/{camp_id}",
        json={"inbox_engine": "browser"},
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["inbox_engine"] == "browser", f"inbox_engine changed unexpectedly: {body['inbox_engine']}"
    assert body["scrape_cursor"] == "ABC", (
        f"DEFECT: cursor was reset even though engine did not change. "
        f"Got: {body['scrape_cursor']!r} (expected 'ABC'). "
        f"engine_switch_resets_cursor('browser','browser') must return False."
    )


# ---------- B-3: active states → engine switch must be blocked (400) --------

@pytest.mark.parametrize("blocked_status", [
    CampaignStatus.listing,
    CampaignStatus.running,
    CampaignStatus.scraping,
])
def test_engine_switch_blocked_while_active(client, _temp_db, blocked_status):
    """
    Active states (listing / running / scraping) must reject inbox_engine change
    with 400. Changing engine mid-run would cause the worker to pick up a cursor
    from the wrong engine type on the very next iteration.
    """
    _, sf = _temp_db
    camp = _make_campaign(
        name=f"B3-blocked-{blocked_status.value}",
        status=blocked_status,
        scrape_cursor="XYZ",
        inbox_engine="browser",
    )
    camp_id = camp.id

    async def _seed(db):
        db.add(camp)
        await db.commit()

    _run(sf, _seed)

    resp = client.put(
        f"/api/campaigns/{camp_id}",
        json={"inbox_engine": "api"},
    )
    assert resp.status_code == 400, (
        f"DEFECT: engine switch allowed in status={blocked_status.value!r}. "
        f"Got {resp.status_code}: {resp.text}. "
        f"Must return 400 — engine switch while active corrupts the cursor."
    )
    detail = resp.json().get("detail", "")
    # Check the right guard fired, not some unrelated 400.
    assert any(kw in detail.lower() for kw in ("engine", "draft", "paused", "ferma")), (
        f"400 came from an unexpected guard (not the engine-switch guard): {detail}"
    )


# ---------- B-4: additional active states that must also be blocked ----------

@pytest.mark.parametrize("blocked_status", [
    CampaignStatus.listing_break,
    CampaignStatus.scraping_break,
    CampaignStatus.scraping_and_running,
])
def test_engine_switch_blocked_in_break_and_parallel_states(client, _temp_db, blocked_status):
    """
    Break and parallel-run states are not in the allowed set (draft/ready/paused/error).
    Changing engine here would be just as dangerous as during active scraping.
    """
    _, sf = _temp_db
    camp = _make_campaign(
        name=f"B4-blocked-{blocked_status.value}",
        status=blocked_status,
        scrape_cursor="DEF",
        inbox_engine="browser",
    )
    camp_id = camp.id

    async def _seed(db):
        db.add(camp)
        await db.commit()

    _run(sf, _seed)

    resp = client.put(
        f"/api/campaigns/{camp_id}",
        json={"inbox_engine": "api"},
    )
    assert resp.status_code == 400, (
        f"DEFECT: engine switch allowed in status={blocked_status.value!r}. "
        f"Got {resp.status_code}: {resp.text}."
    )


# ---------- B-5: allowed states (draft, ready, error) ------------------------

@pytest.mark.parametrize("allowed_status", [
    CampaignStatus.draft,
    CampaignStatus.ready,
    CampaignStatus.error,
])
def test_engine_switch_allowed_in_stopped_states(client, _temp_db, allowed_status):
    """
    draft / ready / error must allow inbox_engine changes (→ 200).
    These are the safe states where no worker holds a cursor.

    'error' was previously blocked by the outer gate (lines 259-264) because it
    was not in {draft, ready, paused}. Fixed by adding 'inbox_engine' to
    always_editable so it bypasses the outer gate and reaches its own inner guard
    (line 285) which correctly lists draft/ready/paused/error as allowed.
    """
    _, sf = _temp_db
    camp = _make_campaign(
        name=f"B5-allowed-{allowed_status.value}",
        status=allowed_status,
        scrape_cursor=None,
        inbox_engine="browser",
    )
    camp_id = camp.id

    async def _seed(db):
        db.add(camp)
        await db.commit()

    _run(sf, _seed)

    resp = client.put(
        f"/api/campaigns/{camp_id}",
        json={"inbox_engine": "api"},
    )
    assert resp.status_code == 200, (
        f"Expected 200 for status={allowed_status.value!r}, "
        f"got {resp.status_code}: {resp.text}"
    )
    assert resp.json()["inbox_engine"] == "api"


# ---------- B-6: engine switch does NOT clobber other fields -----------------

def test_engine_switch_does_not_clobber_name(client, _temp_db):
    """
    Set name='Original Name' first. Then PUT inbox_engine='api' only.
    The name must survive — update_campaign applies fields individually
    (if data.name is not None) so an omitted name must not be cleared.
    """
    _, sf = _temp_db

    # Create via API so we get a real campaign with a valid name.
    resp_create = client.post(
        "/api/campaigns",
        json={
            "name": "Original Name",
            "source_type": "scrape",
            "target_username": "some_target",
            "messaging_enabled": False,
            "inbox_engine": "browser",
        },
    )
    assert resp_create.status_code == 201, resp_create.text
    camp_id = resp_create.json()["id"]

    # Now switch engine only (no name in payload).
    resp_update = client.put(
        f"/api/campaigns/{camp_id}",
        json={"inbox_engine": "api"},
    )
    assert resp_update.status_code == 200, resp_update.text
    body = resp_update.json()
    assert body["inbox_engine"] == "api", f"inbox_engine not updated: {body['inbox_engine']}"
    assert body["name"] == "Original Name", (
        f"DEFECT: name was clobbered by an engine-only update. "
        f"Got: {body['name']!r}"
    )


def test_engine_switch_does_not_clobber_template(client, _temp_db):
    """
    Create campaign with base_message_template set. Then PUT inbox_engine only.
    Template must survive — base_message_template uses model_fields_set guard
    so it is only written if explicitly included in the request.
    """
    _, sf = _temp_db

    resp_create = client.post(
        "/api/campaigns",
        json={
            "name": "Template Survive Test",
            "source_type": "scrape",
            "target_username": "some_target",
            "messaging_enabled": True,
            "base_message_template": "Hello this is my outreach message for you",
            "inbox_engine": "browser",
        },
    )
    assert resp_create.status_code == 201, resp_create.text
    camp_id = resp_create.json()["id"]
    original_template = resp_create.json()["base_message_template"]

    resp_update = client.put(
        f"/api/campaigns/{camp_id}",
        json={"inbox_engine": "api"},
    )
    assert resp_update.status_code == 200, resp_update.text
    body = resp_update.json()
    assert body["inbox_engine"] == "api"
    assert body["base_message_template"] == original_template, (
        f"DEFECT: template clobbered by engine-only update. "
        f"Got: {body['base_message_template']!r}, expected: {original_template!r}"
    )


# ---------- B-7: completed state → engine switch must be blocked -------------

def test_engine_switch_blocked_in_completed_state(client, _temp_db):
    """
    completed is not in the allowed set (draft/ready/paused/error).
    A completed campaign has no cursor to protect, but the guard should
    still fire consistently — completed campaigns can only be updated for
    messaging fields, not engine settings.

    Note: update_campaign has a special path for completed campaigns:
    it allows certain 'completed_message_fields' but inbox_engine is NOT
    in that set, so the outer status guard fires and returns 400.
    """
    _, sf = _temp_db
    camp = _make_campaign(
        name="B7-completed-blocked",
        status=CampaignStatus.completed,
        scrape_cursor=None,
        inbox_engine="browser",
    )
    camp_id = camp.id

    async def _seed(db):
        db.add(camp)
        await db.commit()

    _run(sf, _seed)

    resp = client.put(
        f"/api/campaigns/{camp_id}",
        json={"inbox_engine": "api"},
    )
    assert resp.status_code == 400, (
        f"DEFECT: engine switch allowed on completed campaign. "
        f"Got {resp.status_code}: {resp.text}"
    )


# ---------- B-8: cursor is reset even when engine switch happens in 'ready' --

def test_engine_switch_in_ready_with_existing_cursor_resets_it(client, _temp_db):
    """
    A campaign in 'ready' state can have a leftover cursor from a previous
    listing phase. Switching engine in ready must STILL reset the cursor.
    """
    _, sf = _temp_db
    camp = _make_campaign(
        name="B8-ready-cursor-reset",
        status=CampaignStatus.ready,
        scrape_cursor="LEFTOVER_CURSOR",
        inbox_engine="api",
    )
    camp_id = camp.id

    async def _seed(db):
        db.add(camp)
        await db.commit()

    _run(sf, _seed)

    resp = client.put(
        f"/api/campaigns/{camp_id}",
        json={"inbox_engine": "browser"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["inbox_engine"] == "browser"
    assert body["scrape_cursor"] is None, (
        f"DEFECT: cursor not reset on engine switch in 'ready' state. "
        f"Got: {body['scrape_cursor']!r}"
    )
