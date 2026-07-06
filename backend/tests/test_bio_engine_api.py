"""
bio_engine end-to-end via API: create persists it, update guards it to draft-only.

Mirrors the pattern in test_inbox_engine_switch_adversarial.py — module-scoped
temp SQLite, dependency_overrides for get_db + get_current_user, TestClient
hitting the real FastAPI routes (not the DB directly), so we exercise the exact
code path a real frontend request would hit.
"""

import asyncio
import os
import tempfile
import uuid
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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


# ---------- Fixtures (module-scoped temp SQLite, mirrors engine-switch adversarial) --

@pytest.fixture(scope="module")
def _temp_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="e2e_bio_engine_")
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
            id="00000000-0000-0000-0000-000000000003",
            email="admin2@test.local",
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


def _make_campaign(
    *,
    name: str,
    status: CampaignStatus,
    bio_engine: str = "api",
) -> Campaign:
    return Campaign(
        id=str(uuid.uuid4()),
        name=name,
        source_type="scrape",
        target_username="target_user",
        scrape_mode="followers",
        bio_engine=bio_engine,
        status=status,
        messaging_enabled=False,
    )


# ---------- create: bio_engine persisted -------------------------------------

def test_create_campaign_persists_bio_engine(client):
    resp = client.post(
        "/api/campaigns",
        json={
            "name": "Bio Engine Create Test",
            "source_type": "scrape",
            "target_username": "some_target",
            "messaging_enabled": False,
            "bio_engine": "browser",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["bio_engine"] == "browser", f"bio_engine not persisted: {body.get('bio_engine')!r}"


def test_create_campaign_defaults_bio_engine_to_api(client):
    resp = client.post(
        "/api/campaigns",
        json={
            "name": "Bio Engine Default Test",
            "source_type": "scrape",
            "target_username": "some_target",
            "messaging_enabled": False,
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["bio_engine"] == "api"


# ---------- update: allowed only in draft ------------------------------------

def test_update_bio_engine_allowed_in_draft(client, _temp_db):
    _, sf = _temp_db
    camp = _make_campaign(name="Draft Bio Switch", status=CampaignStatus.draft, bio_engine="api")
    camp_id = camp.id

    async def _seed(db):
        db.add(camp)
        await db.commit()

    _run(sf, _seed)

    resp = client.put(f"/api/campaigns/{camp_id}", json={"bio_engine": "browser"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["bio_engine"] == "browser"


@pytest.mark.parametrize("blocked_status", [
    CampaignStatus.scraping,
    CampaignStatus.scraping_break,
    CampaignStatus.ready,
    CampaignStatus.paused,
    CampaignStatus.running,
    CampaignStatus.scraping_and_running,
    CampaignStatus.listing,
    CampaignStatus.completed,
    CampaignStatus.error,
])
def test_update_bio_engine_rejected_outside_draft(client, _temp_db, blocked_status):
    """
    bio_engine change must be rejected once a campaign has left 'draft' — a
    scraping campaign already has bio workers/fan-out assuming one engine
    (browser fan-out enqueues per-account tasks; switching under it mid-run
    would leave orphaned/duplicated work). Guard is intentionally stricter
    than inbox_engine's (draft/ready/paused/error) since there is no cursor
    to reconcile, just "has this campaign started or not".
    """
    _, sf = _temp_db
    camp = _make_campaign(
        name=f"Blocked-{blocked_status.value}", status=blocked_status, bio_engine="api"
    )
    camp_id = camp.id

    async def _seed(db):
        db.add(camp)
        await db.commit()

    _run(sf, _seed)

    resp = client.put(f"/api/campaigns/{camp_id}", json={"bio_engine": "browser"})
    assert resp.status_code == 400, (
        f"DEFECT: bio_engine switch allowed in status={blocked_status.value!r}. "
        f"Got {resp.status_code}: {resp.text}"
    )
    detail = resp.json().get("detail", "")
    assert any(kw in detail.lower() for kw in ("bio", "draft", "ferma")), (
        f"400 came from an unexpected guard (not the bio_engine guard): {detail}"
    )


def test_update_bio_engine_does_not_clobber_name(client):
    """An update carrying only bio_engine must not touch unrelated fields."""
    resp_create = client.post(
        "/api/campaigns",
        json={
            "name": "Bio Engine No Clobber",
            "source_type": "scrape",
            "target_username": "some_target",
            "messaging_enabled": False,
            "bio_engine": "api",
        },
    )
    assert resp_create.status_code == 201, resp_create.text
    camp_id = resp_create.json()["id"]

    resp_update = client.put(f"/api/campaigns/{camp_id}", json={"bio_engine": "browser"})
    assert resp_update.status_code == 200, resp_update.text
    body = resp_update.json()
    assert body["bio_engine"] == "browser"
    assert body["name"] == "Bio Engine No Clobber"
