"""
End-to-end API tests for the "advanced scraping & contacts" feature set.

Hermetic + deterministic: runs the real FastAPI app against a throwaway
SQLite database (temp file). No Redis, no Instagram, no Supabase.

Wiring:
  - `get_db` is overridden to use a temp-file async SQLite engine whose schema
    is created from the ORM models (`Base.metadata.create_all`). The models
    already define the new columns (messaging_enabled, scrape_daily_limit,
    phone/email/whatsapp on global_contacts, ...) so no Alembic is needed.
  - `get_current_user` is overridden to return a synthetic admin User, so the
    router-level auth dependency (`_protected`) is satisfied without a JWT.

Covered cases (see Step 2 of the QA brief):
  1. Create a scraping-only campaign (messaging_enabled=False, no template) → 201.
  2. Validation: messaging_enabled=True + no template → 422.
  3. Start guard: scraping-only campaign forced to `ready` → POST /start → 400
     "Messaggistica disattivata" (fires before the Redis check).
  4. Leads list/export filters: has_phone returns only phone-bearing lead;
     export CSV header includes phone,email,whatsapp.
  5. A completed scraping-only campaign can be converted to `ready` by enabling
     messaging with a valid template.
  6. Resume cannot bypass messaging_enabled=False on completed lead-only campaigns.
"""
import asyncio
import tempfile
import os
import uuid
import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.database import Base, get_db
from app.utils.auth_deps import get_current_user
from app.models.user import User
from app.models.campaign import Campaign, CampaignStatus
from app.models.global_contact import GlobalContact
# Import all model modules so every table is registered on Base.metadata.
import app.models.account  # noqa: F401
import app.models.campaign_account  # noqa: F401
import app.models.follower  # noqa: F401
import app.models.message  # noqa: F401
import app.models.activity_log  # noqa: F401
import app.models.global_contact  # noqa: F401
import app.models.user  # noqa: F401


# --------------------------------------------------------------------------
# Temp SQLite engine + session factory shared by the test module
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="e2e_advscrape_")
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
def client(temp_db):
    engine, session_factory = temp_db

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

    # Import the app late so engine creation above is unaffected.
    from app.main import app

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _override_get_current_user

    # Do NOT enter the lifespan: the app's startup hook (`_sync_daily_message_counts`,
    # warmup advance) talks to the REAL configured DB (Supabase via .env) through the
    # un-overridable module-level `AsyncSessionLocal`, which would fail/mutate prod.
    # Instantiating TestClient WITHOUT the `with` block skips startup/shutdown while
    # still serving requests. raise_server_exceptions surfaces real 500s.
    c = TestClient(app, raise_server_exceptions=True)
    yield c

    app.dependency_overrides.clear()


def _run(session_factory, coro_fn):
    """Run an async seeding/inspection helper against the temp DB."""
    async def _wrap():
        async with session_factory() as db:
            return await coro_fn(db)
    return asyncio.run(_wrap())


# --------------------------------------------------------------------------
# Case 1 — create a scraping-only campaign
# --------------------------------------------------------------------------
def test_create_scraping_only_campaign(client):
    resp = client.post("/api/campaigns", json={
        "name": "Scraping only",
        "source_type": "scrape",
        "target_username": "targetshop",
        "messaging_enabled": False,
        # intentionally NO base_message_template
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["messaging_enabled"] is False
    assert body["base_message_template"] is None
    assert body["source_type"] == "scrape"
    assert body["target_username"] == "targetshop"


# --------------------------------------------------------------------------
# Case 2 — validation: messaging on but no template → 422
# --------------------------------------------------------------------------
def test_messaging_enabled_without_template_rejected(client):
    resp = client.post("/api/campaigns", json={
        "name": "Bad campaign",
        "source_type": "scrape",
        "target_username": "targetshop",
        "messaging_enabled": True,
        # NO base_message_template → model_validator must reject
    })
    assert resp.status_code == 422, resp.text


# --------------------------------------------------------------------------
# Case 3 — start guard rejects messaging-disabled campaign (before Redis)
# --------------------------------------------------------------------------
def test_start_guard_rejects_messaging_disabled(client, temp_db):
    _, session_factory = temp_db

    # Create the scraping-only campaign via API, then force status=ready in DB.
    resp = client.post("/api/campaigns", json={
        "name": "Scraping only ready",
        "source_type": "scrape",
        "target_username": "anothershop",
        "messaging_enabled": False,
    })
    assert resp.status_code == 201, resp.text
    campaign_id = resp.json()["id"]

    async def _force_ready(db):
        c = await db.get(Campaign, campaign_id)
        c.status = CampaignStatus.ready
        await db.commit()
    _run(session_factory, _force_ready)

    resp = client.post(f"/api/campaigns/{campaign_id}/start")
    # Guard order in start_campaign: status check → messaging_enabled guard → ...
    # → Redis check. messaging_enabled=False fires the 400 BEFORE Redis is touched.
    assert resp.status_code == 400, resp.text
    assert "Messaggistica disattivata" in resp.json()["detail"]


def test_completed_scraping_only_can_be_converted_to_ready(client, temp_db):
    _, session_factory = temp_db

    resp = client.post("/api/campaigns", json={
        "name": "Lead only completed",
        "source_type": "scrape",
        "target_username": "leadshop",
        "messaging_enabled": False,
    })
    assert resp.status_code == 201, resp.text
    campaign_id = resp.json()["id"]

    async def _force_completed(db):
        c = await db.get(Campaign, campaign_id)
        c.status = CampaignStatus.completed
        c.scrape_completed_at = datetime.utcnow()
        c.messages_pending = 12
        await db.commit()
    _run(session_factory, _force_completed)

    resp = client.put(f"/api/campaigns/{campaign_id}", json={
        "messaging_enabled": True,
        "base_message_template": "Ciao, ti scrivo per presentarti una proposta dedicata.",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["messaging_enabled"] is True
    assert body["status"] == "ready"


def test_resume_completed_lead_only_rejects_before_dm_workers(client, temp_db, monkeypatch):
    from app.services import campaign_control

    async def _redis_ok():
        return True

    monkeypatch.setattr(campaign_control, "check_redis_reachable", _redis_ok)
    _, session_factory = temp_db

    resp = client.post("/api/campaigns", json={
        "name": "Lead only resume blocked",
        "source_type": "scrape",
        "target_username": "blockedshop",
        "messaging_enabled": False,
    })
    assert resp.status_code == 201, resp.text
    campaign_id = resp.json()["id"]

    async def _force_completed(db):
        c = await db.get(Campaign, campaign_id)
        c.status = CampaignStatus.completed
        c.scrape_completed_at = datetime.utcnow()
        c.messages_pending = 4
        await db.commit()
    _run(session_factory, _force_completed)

    resp = client.post(f"/api/campaigns/{campaign_id}/resume")
    assert resp.status_code == 400, resp.text
    assert "Messaggistica disattivata" in resp.json()["detail"]


# --------------------------------------------------------------------------
# Case 4 — leads list + export contact filters (has_phone)
# --------------------------------------------------------------------------
def _seed_leads(session_factory):
    async def _seed(db):
        now = datetime.utcnow()
        with_phone = GlobalContact(
            id=str(uuid.uuid4()),
            ig_user_id=911000001,
            username="lead_with_phone",
            full_name="Phone Lead",
            biography="call me",
            phone="+391234567890",
            email=None,
            whatsapp=None,
            scrape_sources=json.dumps([
                {"campaign_id": "c1", "scraping_account_id": "accA",
                 "scraping_account_username": "scraper_a"}
            ]),
            last_contacted_at=now,
            created_at=now,
        )
        without_phone = GlobalContact(
            id=str(uuid.uuid4()),
            ig_user_id=911000002,
            username="lead_no_phone",
            full_name="No Phone Lead",
            biography="no number here",
            phone=None,
            email="someone@example.com",
            whatsapp=None,
            scrape_sources=json.dumps([
                {"campaign_id": "c2", "scraping_account_id": "accB",
                 "scraping_account_username": "scraper_b"}
            ]),
            last_contacted_at=now,
            created_at=now,
        )
        db.add_all([with_phone, without_phone])
        await db.commit()
    _run(session_factory, _seed)


def test_leads_has_phone_filter_list(client, temp_db):
    _, session_factory = temp_db
    _seed_leads(session_factory)

    resp = client.get("/api/leads", params={"has_phone": "true"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    usernames = [item["username"] for item in body["items"]]
    assert "lead_with_phone" in usernames
    assert "lead_no_phone" not in usernames
    # Each returned lead must actually carry a phone.
    for item in body["items"]:
        assert item["phone"]


def test_leads_export_has_phone_filter_csv(client):
    # Relies on the rows seeded by the previous test (module-scoped DB).
    resp = client.get("/api/leads/export", params={"has_phone": "true"})
    assert resp.status_code == 200, resp.text
    csv_text = resp.text
    header = csv_text.splitlines()[0]
    # New contact columns must be present in the export header.
    assert "phone" in header
    assert "email" in header
    assert "whatsapp" in header
    # Only the phone-bearing lead is exported.
    assert "lead_with_phone" in csv_text
    assert "lead_no_phone" not in csv_text
