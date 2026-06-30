"""
Adversarial tests for the "exactly 1 account" guard on dm_threads campaigns.

Part A — pure helper edge cases for inbox_account_count_ok().
Part B — endpoint-level bypass attempts via TestClient + SQLite fixture.

Design notes:
- Part A needs no DB: it tests the pure function inbox_account_count_ok directly.
- Part B reuses the same TestClient + temp SQLite pattern as test_e2e_advanced_scraping.py.
  It seeds state directly into the DB to reach scenarios the API guard would normally prevent.
"""

import asyncio
import os
import tempfile
import uuid
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.campaigns import inbox_account_count_ok

# ============================================================================
# PART A — pure helper edge cases
# ============================================================================


# --- dm_threads: known-good baseline ---

def test_dm_threads_exact_1_is_valid():
    """The only valid case for dm_threads: exactly one account (int 1)."""
    assert inbox_account_count_ok("dm_threads", 1) is True


# --- dm_threads: invalid integer counts ---

def test_dm_threads_count_0_is_invalid():
    """0 accounts: no account assigned — must be blocked."""
    assert inbox_account_count_ok("dm_threads", 0) is False


def test_dm_threads_count_2_is_invalid():
    """2 accounts: one too many — must be blocked."""
    assert inbox_account_count_ok("dm_threads", 2) is False


def test_dm_threads_negative_count_minus_1_is_invalid():
    """
    Negative counts should never happen from a DB scalar, but the guard must
    not accidentally treat them as valid. -1 != 1, so this should be False.
    """
    assert inbox_account_count_ok("dm_threads", -1) is False


def test_dm_threads_negative_count_minus_999_is_invalid():
    """Extreme negative: still not 1, must be False."""
    assert inbox_account_count_ok("dm_threads", -999) is False


def test_dm_threads_very_large_count_is_invalid():
    """Large positive counts (e.g. 999999) are not 1 and must be blocked."""
    assert inbox_account_count_ok("dm_threads", 999999) is False


# --- dm_threads: bool subtype footgun (True == 1, False == 0 in Python) ---

def test_dm_threads_bool_True_is_treated_as_1_footgun():
    """
    FOOTGUN: In Python, `True == 1` evaluates to True. The current
    implementation uses `active_count == 1`, which means
    inbox_account_count_ok('dm_threads', True) returns True.

    This is probably harmless in practice (the SQLAlchemy scalar() always
    returns an int, not a bool), but it is a type-system surprise worth
    documenting. If this test fails in the future, it means the guard
    has been tightened with an isinstance check — which is the safer direction.
    """
    # Document actual behavior. We assert True here because the function
    # currently passes bool True through (True == 1 in Python). If you want
    # strict int-only behavior, change the guard to `active_count == 1 and
    # isinstance(active_count, int)` and flip this to `is False`.
    result = inbox_account_count_ok("dm_threads", True)
    # True == 1, so the equality passes — guard treats bool True as 1 account.
    assert result is True, (
        "KNOWN FOOTGUN: bool True passes as count==1. "
        "This is not a live defect (DB never returns bool), but documents "
        "that the guard lacks isinstance protection."
    )


def test_dm_threads_bool_False_is_treated_as_0_footgun():
    """
    FOOTGUN companion: bool False == 0, so it is correctly rejected.
    The risk is symmetric with True: if a caller accidentally passes a bool,
    False is safe (rejected), but True is silently accepted.
    """
    result = inbox_account_count_ok("dm_threads", False)
    # False == 0, not 1, so the guard returns False (blocked) — correct.
    assert result is False, (
        "bool False should be treated as 0 and rejected."
    )


# --- scrape_mode edge cases ---

def test_none_scrape_mode_is_not_constrained():
    """
    scrape_mode=None: no constraint is applied (non-dm_threads path).
    The function must not raise and must return True.
    """
    assert inbox_account_count_ok(None, 0) is True
    assert inbox_account_count_ok(None, 5) is True


def test_wrong_case_DM_THREADS_is_not_constrained():
    """
    'DM_THREADS' (uppercase) is not 'dm_threads' — the guard is case-sensitive.
    Any count is allowed, which means a typo in the caller could bypass the guard.
    This test documents that exact case-sensitivity is relied upon.
    """
    assert inbox_account_count_ok("DM_THREADS", 0) is True
    assert inbox_account_count_ok("DM_THREADS", 5) is True


def test_trailing_space_scrape_mode_is_not_constrained():
    """
    'dm_threads ' (trailing space) is not equal to 'dm_threads'.
    If the DB field ever stores a value with trailing whitespace, the guard
    silently becomes a no-op. Documents reliance on clean string storage.
    """
    assert inbox_account_count_ok("dm_threads ", 0) is True
    assert inbox_account_count_ok("dm_threads ", 5) is True


def test_empty_string_scrape_mode_is_not_constrained():
    """Empty string is not 'dm_threads' — guard does not apply, all counts pass."""
    assert inbox_account_count_ok("", 0) is True
    assert inbox_account_count_ok("", 5) is True


def test_other_modes_not_constrained_followers():
    """Regression: followers mode must never be constrained regardless of count."""
    assert inbox_account_count_ok("followers", 0) is True
    assert inbox_account_count_ok("followers", 1) is True
    assert inbox_account_count_ok("followers", 10) is True


def test_other_modes_not_constrained_following():
    """Regression: following mode must never be constrained."""
    assert inbox_account_count_ok("following", 0) is True
    assert inbox_account_count_ok("following", 2) is True


# ============================================================================
# PART B — endpoint bypass attempts
# ============================================================================
# Fixture mirrors test_e2e_advanced_scraping.py exactly so the harness is proven.
# We need import here so all ORM tables are registered on Base.metadata.

import app.models.account  # noqa: F401
import app.models.activity_log  # noqa: F401
import app.models.campaign_account  # noqa: F401
import app.models.follower  # noqa: F401
import app.models.global_contact  # noqa: F401
import app.models.imported_profile  # noqa: F401
import app.models.message  # noqa: F401
import app.models.user  # noqa: F401

from app.database import Base, get_db
from app.models.account import AccountStatus, InstagramAccount
from app.models.campaign import Campaign, CampaignStatus
from app.models.campaign_account import CampaignAccount
from app.models.user import User
from app.utils.auth_deps import get_current_user


# --------------------------------------------------------------------------
# Shared SQLite fixture (module-scoped for speed)
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def _temp_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="e2e_inbox_adv_")
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
            id="00000000-0000-0000-0000-000000000001",
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
    """Run an async helper against the temp DB (mirrors e2e helper)."""
    async def _wrap():
        async with session_factory() as db:
            return await coro_fn(db)
    return asyncio.run(_wrap())


# --------------------------------------------------------------------------
# Helpers: seed reusable rows
# --------------------------------------------------------------------------

def _make_account(username: str, status: AccountStatus = AccountStatus.active) -> InstagramAccount:
    return InstagramAccount(
        id=str(uuid.uuid4()),
        username=username,
        encrypted_password="enc_placeholder",
        status=status,
        daily_message_limit=20,
    )


def _seed_campaign_and_account(
    session_factory,
    campaign_name: str,
    scrape_mode: str,
    acc_username: str,
    acc_status: AccountStatus = AccountStatus.active,
) -> tuple[str, str]:
    """Create one campaign + one Instagram account; return (campaign_id, account_id)."""
    camp_id = str(uuid.uuid4())
    acc_id = str(uuid.uuid4())

    async def _seed(db):
        camp = Campaign(
            id=camp_id,
            name=campaign_name,
            source_type="scrape",
            target_username="target_user",
            scrape_mode=scrape_mode,
            status=CampaignStatus.draft,
        )
        acc = InstagramAccount(
            id=acc_id,
            username=acc_username,
            encrypted_password="enc_placeholder",
            status=acc_status,
            daily_message_limit=20,
        )
        db.add_all([camp, acc])
        await db.commit()

    _run(session_factory, _seed)
    return camp_id, acc_id


# --------------------------------------------------------------------------
# B-1: assign 1st account to dm_threads → 2xx; assign 2nd → 400
# --------------------------------------------------------------------------

def test_assign_first_account_dm_threads_succeeds(client, _temp_db):
    """
    Assigning the first account to a dm_threads campaign must succeed (201).
    This is the happy path for the guard.
    """
    _, sf = _temp_db
    camp_id, acc_id = _seed_campaign_and_account(
        sf, "B1-DM-campaign", "dm_threads", "acc_b1_first"
    )

    resp = client.post(
        f"/api/campaigns/{camp_id}/accounts",
        json={"account_id": acc_id, "role": "both"},
        params={"force": "true"},
    )
    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"


def test_assign_second_inbox_account_dm_threads_rejected(client, _temp_db):
    """
    NEW MODEL: only ONE inbox-capable account per campaign. A first inbox
    account is assigned; assigning a SECOND inbox-capable account must 400.
    (Non-inbox accounts are unlimited — see the next test.)
    """
    _, sf = _temp_db
    camp_id = str(uuid.uuid4())
    acc1_id = str(uuid.uuid4())
    acc2_id = str(uuid.uuid4())

    async def _seed(db):
        camp = Campaign(
            id=camp_id,
            name="B1-second-inbox-block",
            source_type="scrape",
            target_username="target2",
            scrape_mode="dm_threads",
            status=CampaignStatus.draft,
        )
        acc1 = InstagramAccount(
            id=acc1_id, username="acc_b1_a",
            encrypted_password="x", status=AccountStatus.active, daily_message_limit=20,
        )
        acc2 = InstagramAccount(
            id=acc2_id, username="acc_b1_b",
            encrypted_password="x", status=AccountStatus.active, daily_message_limit=20,
        )
        # Pre-seed: first account already carries the inbox capability
        ca = CampaignAccount(
            campaign_id=camp_id, account_id=acc1_id, is_active=True, role="inbox_both"
        )
        db.add_all([camp, acc1, acc2, ca])
        await db.commit()

    _run(sf, _seed)

    resp = client.post(
        f"/api/campaigns/{camp_id}/accounts",
        json={"account_id": acc2_id, "role": "inbox"},
        params={"force": "true"},
    )
    assert resp.status_code == 400, (
        f"Expected 400 (second inbox account blocked), got {resp.status_code}: {resp.text}"
    )
    assert "inbox" in resp.json()["detail"].lower(), (
        f"Error detail does not mention inbox: {resp.json()['detail']}"
    )


def test_assign_second_non_inbox_account_dm_threads_allowed(client, _temp_db):
    """
    NEW MODEL: with the inbox account already present, adding extra
    scraping/dm accounts to the SAME dm_threads campaign must succeed (201).
    This is the whole point of the feature: spread bio/DM across accounts.
    """
    _, sf = _temp_db
    camp_id = str(uuid.uuid4())
    acc1_id = str(uuid.uuid4())
    acc2_id = str(uuid.uuid4())

    async def _seed(db):
        camp = Campaign(
            id=camp_id,
            name="B1-second-scraper-ok",
            source_type="scrape",
            target_username="target2b",
            scrape_mode="dm_threads",
            status=CampaignStatus.draft,
        )
        acc1 = InstagramAccount(
            id=acc1_id, username="acc_b1c_inbox",
            encrypted_password="x", status=AccountStatus.active, daily_message_limit=20,
        )
        acc2 = InstagramAccount(
            id=acc2_id, username="acc_b1c_scraper",
            encrypted_password="x", status=AccountStatus.active, daily_message_limit=20,
        )
        ca = CampaignAccount(
            campaign_id=camp_id, account_id=acc1_id, is_active=True, role="inbox"
        )
        db.add_all([camp, acc1, acc2, ca])
        await db.commit()

    _run(sf, _seed)

    resp = client.post(
        f"/api/campaigns/{camp_id}/accounts",
        json={"account_id": acc2_id, "role": "scraping"},
        params={"force": "true"},
    )
    assert resp.status_code == 201, (
        f"Expected 201 (extra scraping account allowed), got {resp.status_code}: {resp.text}"
    )


# --------------------------------------------------------------------------
# B-2: INACTIVE first account — does the assignment guard count it?
#       If the count includes inactive rows, a 2nd assign is correctly blocked.
#       If it counts only active rows, a 2nd account could slip in.
# --------------------------------------------------------------------------

def test_assign_second_inbox_when_first_inbox_is_inactive_rejected(client, _temp_db):
    """
    ADVERSARIAL: the first INBOX assignment is present but is_active=False.
    The cap counts ALL inbox rows (active+inactive), so a second inbox account
    MUST still be rejected (400) — you can't bypass by deactivating the first.

    If this fails (201), the cap only counts active rows and a disabled inbox
    account would let a second one slip in.
    """
    _, sf = _temp_db
    camp_id = str(uuid.uuid4())
    acc1_id = str(uuid.uuid4())
    acc2_id = str(uuid.uuid4())

    async def _seed(db):
        camp = Campaign(
            id=camp_id,
            name="B2-inactive-inbox-bypass",
            source_type="scrape",
            target_username="target3",
            scrape_mode="dm_threads",
            status=CampaignStatus.draft,
        )
        acc1 = InstagramAccount(
            id=acc1_id, username="acc_b2_inactive",
            encrypted_password="x", status=AccountStatus.active, daily_message_limit=20,
        )
        acc2 = InstagramAccount(
            id=acc2_id, username="acc_b2_new",
            encrypted_password="x", status=AccountStatus.active, daily_message_limit=20,
        )
        # First inbox account assigned but INACTIVE (is_active=False)
        ca = CampaignAccount(
            campaign_id=camp_id, account_id=acc1_id, is_active=False, role="inbox"
        )
        db.add_all([camp, acc1, acc2, ca])
        await db.commit()

    _run(sf, _seed)

    resp = client.post(
        f"/api/campaigns/{camp_id}/accounts",
        json={"account_id": acc2_id, "role": "inbox"},
        params={"force": "true"},
    )
    # The cap counts ALL inbox rows (including inactive). Must still block.
    assert resp.status_code == 400, (
        f"DEFECT: second inbox account allowed even though a (deactivated) inbox row exists. "
        f"Got {resp.status_code}: {resp.text}. "
        f"The cap must count ALL inbox CampaignAccount rows regardless of is_active."
    )


# --------------------------------------------------------------------------
# B-3: start_list on dm_threads with 0 active accounts → must be 400
# --------------------------------------------------------------------------

def test_start_list_dm_threads_with_zero_accounts_rejected(client, _temp_db):
    """
    start_list on a dm_threads campaign that has no assigned accounts must
    return 400 (inbox guard fires: active_count=0 != 1).

    Note: start_list checks `has_active_role_account` BEFORE the inbox guard.
    With 0 accounts that check also fails (400 "Nessun account attivo").
    Either way the result is 400 — the campaign cannot start.
    """
    _, sf = _temp_db
    camp_id = str(uuid.uuid4())

    async def _seed(db):
        camp = Campaign(
            id=camp_id,
            name="B3-no-account",
            source_type="scrape",
            target_username="target4",
            scrape_mode="dm_threads",
            status=CampaignStatus.draft,
        )
        db.add(camp)
        await db.commit()

    _run(sf, _seed)

    resp = client.post(f"/api/campaigns/{camp_id}/list/start")
    assert resp.status_code == 400, (
        f"Expected 400 (no accounts), got {resp.status_code}: {resp.text}"
    )


# --------------------------------------------------------------------------
# B-4: start_list on dm_threads with 2 active accounts seeded directly via DB
#       (bypassing the assignment API guard) → must be 400
# --------------------------------------------------------------------------

def test_start_list_dm_threads_with_two_inbox_accounts_rejected(client, _temp_db):
    """
    ADVERSARIAL: bypass the assignment API cap by inserting 2 INBOX
    CampaignAccount rows directly into the DB (simulating a migration or race).
    Then call start_list.

    The start_list guard must independently count active INBOX accounts and
    reject if count != 1. If this fails (≠400), start_list relies solely on the
    assignment cap and a DB-level bypass would start with 2 inbox accounts.
    """
    _, sf = _temp_db
    camp_id = str(uuid.uuid4())
    acc1_id = str(uuid.uuid4())
    acc2_id = str(uuid.uuid4())

    async def _seed(db):
        camp = Campaign(
            id=camp_id,
            name="B4-two-inbox-bypass",
            source_type="scrape",
            target_username="target5",
            scrape_mode="dm_threads",
            status=CampaignStatus.draft,
        )
        acc1 = InstagramAccount(
            id=acc1_id, username="acc_b4_one",
            encrypted_password="x", status=AccountStatus.active, daily_message_limit=20,
        )
        acc2 = InstagramAccount(
            id=acc2_id, username="acc_b4_two",
            encrypted_password="x", status=AccountStatus.active, daily_message_limit=20,
        )
        # Insert BOTH inbox-capable directly — bypassing the assign API cap
        ca1 = CampaignAccount(
            campaign_id=camp_id, account_id=acc1_id, is_active=True, role="inbox"
        )
        ca2 = CampaignAccount(
            campaign_id=camp_id, account_id=acc2_id, is_active=True, role="inbox_both"
        )
        db.add_all([camp, acc1, acc2, ca1, ca2])
        await db.commit()

    _run(sf, _seed)

    resp = client.post(f"/api/campaigns/{camp_id}/list/start")
    assert resp.status_code == 400, (
        f"DEFECT: start_list allowed a dm_threads campaign with 2 active inbox accounts. "
        f"Got {resp.status_code}: {resp.text}. "
        f"The start_list guard must independently validate inbox_count == 1."
    )
    detail = resp.json().get("detail", "")
    # Check it's the inbox guard, not just the Redis check or some other error
    assert any(kw in detail.lower() for kw in ("inbox", "dm", "account", "esattamente")), (
        f"400 came from an unexpected guard: {detail}"
    )


# --------------------------------------------------------------------------
# B-5: followers-mode campaign with 2 accounts → start_list NOT blocked
#       (no regression: inbox guard must be dm_threads-only)
# --------------------------------------------------------------------------

def test_start_list_followers_mode_two_accounts_not_blocked_by_inbox_guard(client, _temp_db):
    """
    REGRESSION: a followers-mode campaign with 2 active accounts must NOT be
    blocked by the inbox guard. start_list may still fail for other reasons
    (Redis not reachable, etc.) but the inbox guard must not fire.

    We seed 2 accounts directly and call start_list. The expected failure
    mode here is 503 (Redis not reachable) or 400 for a non-inbox reason,
    but NOT a 400 whose detail mentions "inbox" or "esattamente 1".
    """
    _, sf = _temp_db
    camp_id = str(uuid.uuid4())
    acc1_id = str(uuid.uuid4())
    acc2_id = str(uuid.uuid4())

    async def _seed(db):
        camp = Campaign(
            id=camp_id,
            name="B5-followers-two-accounts",
            source_type="scrape",
            target_username="target6",
            scrape_mode="followers",
            status=CampaignStatus.draft,
        )
        acc1 = InstagramAccount(
            id=acc1_id, username="acc_b5_one",
            encrypted_password="x", status=AccountStatus.active, daily_message_limit=20,
        )
        acc2 = InstagramAccount(
            id=acc2_id, username="acc_b5_two",
            encrypted_password="x", status=AccountStatus.active, daily_message_limit=20,
        )
        ca1 = CampaignAccount(
            campaign_id=camp_id, account_id=acc1_id, is_active=True, role="both"
        )
        ca2 = CampaignAccount(
            campaign_id=camp_id, account_id=acc2_id, is_active=True, role="both"
        )
        db.add_all([camp, acc1, acc2, ca1, ca2])
        await db.commit()

    _run(sf, _seed)

    resp = client.post(f"/api/campaigns/{camp_id}/list/start")
    # The inbox guard must NOT fire. Any status is acceptable (200 = started OK,
    # 400 for non-inbox reason, 409 bot-busy, 503 Redis-down) as long as the
    # response does not mention the inbox/dm_threads "esattamente 1" guard.
    if resp.status_code == 400:
        detail = resp.json().get("detail", "")
        assert "inbox" not in detail.lower() and "esattamente" not in detail.lower(), (
            f"REGRESSION: inbox guard fired on a followers-mode campaign with 2 accounts. "
            f"Detail: {detail}"
        )
    # 200 = campaign actually started (TestClient environment has Redis or skips the check).
    # 503/409 = infrastructure not available in test env — still proves inbox guard didn't fire.
    assert resp.status_code in (200, 400, 409, 503), (
        f"Unexpected status {resp.status_code} — check what guard fired: {resp.text}"
    )
