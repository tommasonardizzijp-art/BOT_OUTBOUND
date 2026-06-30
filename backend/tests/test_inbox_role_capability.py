"""End-to-end + unit coverage for the composable inbox-role capability model.

Feature: an account on a dm_threads campaign may carry the *inbox* capability
(reads the DM inbox) combined with scraping and/or DM. Only ONE inbox account
per campaign; scraping/dm accounts are unlimited so the bio/DM work spreads.

Part A — pure capability matrix (app.utils.roles), no DB.
Part B — service-level routing queries on a real SQLite session: the right
         accounts feed the bio pool, the inbox lister, and the DM workers.
Part C — endpoint cap edge cases (assign + PUT-promotion) via TestClient.
"""
import asyncio
import os
import tempfile
import uuid
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.utils.roles import (
    ALL_ROLES,
    SCRAPE_ROLES,
    DM_ROLES,
    INBOX_ROLES,
    can_scrape,
    can_dm,
    is_inbox,
)

# ============================================================================
# PART A — pure capability matrix
# ============================================================================

# (role, can_scrape, can_dm, is_inbox)
_MATRIX = [
    ("scraping",        True,  False, False),
    ("dm",              False, True,  False),
    ("both",            True,  True,  False),
    ("inbox",           False, False, True),
    ("inbox_scraping",  True,  False, True),
    ("inbox_dm",        False, True,  True),
    ("inbox_both",      True,  True,  True),
]


@pytest.mark.parametrize("role,scrape,dm,inbox", _MATRIX)
def test_capability_matrix(role, scrape, dm, inbox):
    assert can_scrape(role) is scrape, f"{role} can_scrape"
    assert can_dm(role) is dm, f"{role} can_dm"
    assert is_inbox(role) is inbox, f"{role} is_inbox"


def test_matrix_covers_every_role():
    assert {row[0] for row in _MATRIX} == set(ALL_ROLES)


def test_all_roles_unique_and_fit_column():
    assert len(ALL_ROLES) == len(set(ALL_ROLES)) == 7
    # campaign_accounts.role is String(16) — every value must fit.
    assert all(len(r) <= 16 for r in ALL_ROLES), [r for r in ALL_ROLES if len(r) > 16]


def test_role_set_relationships():
    # Every inbox-capable role is a valid role.
    assert set(INBOX_ROLES) <= set(ALL_ROLES)
    assert set(SCRAPE_ROLES) <= set(ALL_ROLES)
    assert set(DM_ROLES) <= set(ALL_ROLES)
    # The two inbox combos that scrape/dm must appear in the respective sets.
    assert "inbox_scraping" in SCRAPE_ROLES and "inbox_both" in SCRAPE_ROLES
    assert "inbox_dm" in DM_ROLES and "inbox_both" in DM_ROLES
    # Pure inbox carries none of the work capabilities.
    assert "inbox" not in SCRAPE_ROLES and "inbox" not in DM_ROLES


def test_none_and_unknown_default_to_both():
    # Legacy rows / missing role default to 'both' (scrape+dm, not inbox).
    for missing in (None, ""):
        assert can_scrape(missing) is True
        assert can_dm(missing) is True
        assert is_inbox(missing) is False
    # Unknown garbage is treated as no capability (not 'both').
    assert can_scrape("garbage") is False
    assert can_dm("garbage") is False
    assert is_inbox("garbage") is False


# ============================================================================
# PART B + C — DB-backed. Shared SQLite fixture (mirrors test_inbox_guard_adversarial).
# ============================================================================

import app.models.account  # noqa: F401,E402
import app.models.activity_log  # noqa: F401,E402
import app.models.campaign_account  # noqa: F401,E402
import app.models.follower  # noqa: F401,E402
import app.models.global_contact  # noqa: F401,E402
import app.models.imported_profile  # noqa: F401,E402
import app.models.message  # noqa: F401,E402
import app.models.user  # noqa: F401,E402

from app.database import Base, get_db  # noqa: E402
from app.models.account import AccountStatus, InstagramAccount  # noqa: E402
from app.models.campaign import Campaign, CampaignStatus  # noqa: E402
from app.models.campaign_account import CampaignAccount  # noqa: E402
from app.models.user import User  # noqa: E402
from app.utils.auth_deps import get_current_user  # noqa: E402


@pytest.fixture(scope="module")
def _temp_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="e2e_role_cap_")
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
    async def _wrap():
        async with session_factory() as db:
            return await coro_fn(db)
    return asyncio.run(_wrap())


def _seed_campaign_with_roles(session_factory, name, scrape_mode, roles):
    """Create a campaign + one account per role in `roles`.

    `roles` is a list of role strings; returns (campaign_id, {role: account_id}).
    """
    camp_id = str(uuid.uuid4())
    by_role = {}

    async def _seed(db):
        camp = Campaign(
            id=camp_id, name=name, source_type="scrape",
            target_username="t_" + name, scrape_mode=scrape_mode,
            status=CampaignStatus.draft,
        )
        db.add(camp)
        for i, role in enumerate(roles):
            acc_id = str(uuid.uuid4())
            by_role[role] = acc_id
            db.add(InstagramAccount(
                id=acc_id, username=f"{name}_acc{i}_{role}",
                encrypted_password="x", status=AccountStatus.active, daily_message_limit=20,
            ))
            db.add(CampaignAccount(
                campaign_id=camp_id, account_id=acc_id, is_active=True, role=role,
            ))
        await db.commit()

    _run(session_factory, _seed)
    return camp_id, by_role


# ----------------------------------------------------------------------------
# PART B — routing: which accounts feed each phase
# ----------------------------------------------------------------------------

def test_bio_pool_includes_inbox_scraping_and_both(_temp_db):
    """The bio pool (SCRAPE_ROLES) must include inbox accounts that also scrape,
    and exclude pure-inbox / pure-dm accounts."""
    from app.services.scraper import _eligible_scraping_accounts
    _, sf = _temp_db
    camp_id, by_role = _seed_campaign_with_roles(
        sf, "bio-pool", "dm_threads",
        ["inbox", "inbox_scraping", "inbox_dm", "inbox_both", "scraping", "dm"],
    )

    async def _check(db):
        return await _eligible_scraping_accounts(db, camp_id)

    accounts = _run(sf, _check)
    got = {a.id for a in accounts}
    expected = {by_role["inbox_scraping"], by_role["inbox_both"], by_role["scraping"]}
    assert got == expected, f"bio pool mismatch: got {got}, expected {expected}"


def test_single_inbox_account_picks_the_one_inbox(_temp_db):
    """_single_inbox_account returns the lone inbox account even when several
    non-inbox scraping/dm accounts coexist on the same campaign."""
    from app.services.scrape_inbox import _single_inbox_account
    _, sf = _temp_db
    camp_id, by_role = _seed_campaign_with_roles(
        sf, "single-inbox", "dm_threads",
        ["inbox_both", "scraping", "scraping", "dm"],
    )
    # NB: two 'scraping' keys collapse in the dict, but both rows exist in DB.

    async def _check(db):
        return await _single_inbox_account(db, camp_id)

    acc = _run(sf, _check)
    assert acc.id == by_role["inbox_both"]


def test_single_inbox_account_raises_when_no_inbox(_temp_db):
    """No inbox-capable account → _single_inbox_account raises (can't list DM)."""
    from app.services.scrape_inbox import _single_inbox_account
    from app.utils.exceptions import ScrapeBudgetError
    _, sf = _temp_db
    camp_id, _ = _seed_campaign_with_roles(
        sf, "no-inbox", "dm_threads", ["scraping", "dm", "both"],
    )

    async def _check(db):
        return await _single_inbox_account(db, camp_id)

    with pytest.raises(ScrapeBudgetError):
        _run(sf, _check)


def test_dm_worker_eligibility_includes_inbox_dm(_temp_db):
    """has_active_role_account(DM_ROLES) must see an inbox_dm account (it sends DMs)."""
    from app.services.campaign_control import has_active_role_account
    _, sf = _temp_db
    camp_id, _ = _seed_campaign_with_roles(
        sf, "dm-elig", "dm_threads", ["inbox_dm"],
    )

    async def _check(db):
        return await has_active_role_account(db, camp_id, DM_ROLES)

    assert _run(sf, _check) is True


def test_pure_inbox_is_not_a_dm_or_scrape_account(_temp_db):
    """A campaign whose only account is pure 'inbox' has no DM and no scrape
    account — it can only list the inbox."""
    from app.services.campaign_control import has_active_role_account
    _, sf = _temp_db
    camp_id, _ = _seed_campaign_with_roles(
        sf, "pure-inbox", "dm_threads", ["inbox"],
    )

    async def _check(db):
        has_dm = await has_active_role_account(db, camp_id, DM_ROLES)
        has_scrape = await has_active_role_account(db, camp_id, SCRAPE_ROLES)
        has_inbox = await has_active_role_account(db, camp_id, INBOX_ROLES)
        return has_dm, has_scrape, has_inbox

    has_dm, has_scrape, has_inbox = _run(sf, _check)
    assert (has_dm, has_scrape, has_inbox) == (False, False, True)


# ----------------------------------------------------------------------------
# PART C — assignment cap edge cases (POST + PUT promotion)
# ----------------------------------------------------------------------------

def test_extra_scraping_accounts_unlimited_then_one_inbox(client, _temp_db):
    """1 inbox + 3 scraping accounts all assignable on a dm_threads campaign."""
    _, sf = _temp_db
    camp_id = str(uuid.uuid4())

    async def _seed(db):
        db.add(Campaign(
            id=camp_id, name="C-mixed", source_type="scrape",
            target_username="tc", scrape_mode="dm_threads", status=CampaignStatus.draft,
        ))
        await db.commit()

    _run(sf, _seed)

    def _assign(role):
        acc_id = str(uuid.uuid4())

        async def _mk(db):
            db.add(InstagramAccount(
                id=acc_id, username=f"cmix_{role}_{acc_id[:6]}",
                encrypted_password="x", status=AccountStatus.active, daily_message_limit=20,
            ))
            await db.commit()
        _run(sf, _mk)
        return client.post(
            f"/api/campaigns/{camp_id}/accounts",
            json={"account_id": acc_id, "role": role},
            params={"force": "true"},
        )

    assert _assign("inbox").status_code == 201
    for _ in range(3):
        assert _assign("scraping").status_code == 201
    # A SECOND inbox-capable account must now be blocked.
    assert _assign("inbox_dm").status_code == 400


def test_put_promote_second_account_to_inbox_rejected(client, _temp_db):
    """PUT changing a scraping account's role to an inbox role must 400 when an
    inbox account already exists (cap enforced on update, not only on assign)."""
    _, sf = _temp_db
    camp_id = str(uuid.uuid4())
    inbox_acc = str(uuid.uuid4())
    scr_acc = str(uuid.uuid4())

    async def _seed(db):
        db.add(Campaign(
            id=camp_id, name="C-put-block", source_type="scrape",
            target_username="tcp", scrape_mode="dm_threads", status=CampaignStatus.draft,
        ))
        db.add_all([
            InstagramAccount(id=inbox_acc, username="cput_inbox", encrypted_password="x",
                             status=AccountStatus.active, daily_message_limit=20),
            InstagramAccount(id=scr_acc, username="cput_scr", encrypted_password="x",
                             status=AccountStatus.active, daily_message_limit=20),
            CampaignAccount(campaign_id=camp_id, account_id=inbox_acc, is_active=True, role="inbox"),
            CampaignAccount(campaign_id=camp_id, account_id=scr_acc, is_active=True, role="scraping"),
        ])
        await db.commit()

    _run(sf, _seed)

    resp = client.put(
        f"/api/campaigns/{camp_id}/accounts/{scr_acc}",
        json={"role": "inbox_scraping"},
    )
    assert resp.status_code == 400, f"Expected 400 promoting 2nd inbox, got {resp.status_code}: {resp.text}"
    assert "inbox" in resp.json()["detail"].lower()


def test_put_change_existing_inbox_variant_allowed(client, _temp_db):
    """PUT on the SAME inbox account, switching inbox→inbox_scraping, is allowed
    (it's still one inbox account — the cap excludes the row being updated)."""
    _, sf = _temp_db
    camp_id = str(uuid.uuid4())
    inbox_acc = str(uuid.uuid4())

    async def _seed(db):
        db.add(Campaign(
            id=camp_id, name="C-put-self", source_type="scrape",
            target_username="tcs", scrape_mode="dm_threads", status=CampaignStatus.draft,
        ))
        db.add_all([
            InstagramAccount(id=inbox_acc, username="cputself_inbox", encrypted_password="x",
                             status=AccountStatus.active, daily_message_limit=20),
            CampaignAccount(campaign_id=camp_id, account_id=inbox_acc, is_active=True, role="inbox"),
        ])
        await db.commit()

    _run(sf, _seed)

    resp = client.put(
        f"/api/campaigns/{camp_id}/accounts/{inbox_acc}",
        json={"role": "inbox_both"},
    )
    assert resp.status_code == 200, f"Expected 200 changing own inbox variant, got {resp.status_code}: {resp.text}"
    assert resp.json()["role"] == "inbox_both"


def test_invalid_role_value_rejected(client, _temp_db):
    """Schema validation: an unknown role string is a 422 (not silently stored)."""
    _, sf = _temp_db
    camp_id = str(uuid.uuid4())
    acc_id = str(uuid.uuid4())

    async def _seed(db):
        db.add(Campaign(
            id=camp_id, name="C-badrole", source_type="scrape",
            target_username="tcb", scrape_mode="dm_threads", status=CampaignStatus.draft,
        ))
        db.add(InstagramAccount(id=acc_id, username="cbad", encrypted_password="x",
                                status=AccountStatus.active, daily_message_limit=20))
        await db.commit()

    _run(sf, _seed)

    resp = client.post(
        f"/api/campaigns/{camp_id}/accounts",
        json={"account_id": acc_id, "role": "inbox_messages"},  # not a real role
        params={"force": "true"},
    )
    assert resp.status_code == 422, f"Expected 422 for invalid role, got {resp.status_code}: {resp.text}"
