"""API: nuovi campi in create; campi messaggi/AI editabili anche a campagna running.

Fixture caveat (vedi brief task-5): questo repo NON ha una fixture `db_session` né
un client `httpx.AsyncClient` condiviso in conftest.py — l'unica guardia globale è
DB SQLite di test + Telegram spento (tests/conftest.py). Mirror del pattern reale
già usato per lo stesso router in test_bio_engine_api.py / test_inbox_engine_switch_
adversarial.py: TestClient (sync) su un DB SQLite temp module-scoped, con
dependency_overrides per get_db + get_current_user (il router e' protetto da
Depends(get_current_user) via app.include_router(..., dependencies=_protected) in
app/main.py — un client senza l'override prenderebbe 401/403).
"""

import asyncio
import os
import tempfile
import uuid
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Registra tutte le tabelle ORM su Base.metadata.
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


# ---------- Fixtures (module-scoped temp SQLite, mirror di test_bio_engine_api.py) ----

@pytest.fixture(scope="module")
def _temp_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="tpl_mode_api_")
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
            id="00000000-0000-0000-0000-000000000005",
            email="admin-tplmode@test.local",
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
    base_message_template: str = "Vecchio template abbastanza lungo",
    ai_enabled: bool = False,
    bio_engine: str = "api",
    messaging_enabled: bool = True,
) -> Campaign:
    return Campaign(
        id=str(uuid.uuid4()),
        name=name,
        source_type="scrape",
        target_username="target_user",
        scrape_mode="followers",
        base_message_template=base_message_template,
        ai_enabled=ai_enabled,
        bio_engine=bio_engine,
        status=status,
        messaging_enabled=messaging_enabled,
    )


def _seed(session_factory, campaign: Campaign):
    async def _do(db):
        db.add(campaign)
        await db.commit()
    _run(session_factory, _do)


# ---------- create: nuovi campi accettati e persistiti -------------------------

def test_create_defaults_no_ai(client):
    resp = client.post("/api/campaigns", json={
        "name": "tpl-api",
        "target_username": "acme",
        "base_message_template": "Ciao {nome}, ti scrivo per il progetto",
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["ai_enabled"] is False
    assert body["message_template_c"] is None


def test_create_with_ai_and_template_c(client):
    resp = client.post("/api/campaigns", json={
        "name": "tpl-api2",
        "target_username": "acme",
        "base_message_template": "Ciao {nome}, ti scrivo per il progetto",
        "message_template_c": "Terzo template abbastanza lungo",
        "ai_enabled": True,
        "ai_system_prompt": "Tono formale, max 3 frasi.",
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["ai_enabled"] is True
    assert body["message_template_c"].startswith("Terzo")
    assert body["ai_system_prompt"].startswith("Tono")


# ---------- update: campi messaggi/AI editabili anche a campagna running -------

def test_update_message_fields_while_running(client, _temp_db):
    _, sf = _temp_db
    camp = _make_campaign(name="run", status=CampaignStatus.running, ai_enabled=True)
    _seed(sf, camp)

    resp = client.put(f"/api/campaigns/{camp.id}", json={
        "ai_enabled": False,
        "base_message_template": "Nuovo template abbastanza lungo davvero",
        "message_template_c": "Template C abbastanza lungo pure lui",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ai_enabled"] is False
    assert body["base_message_template"].startswith("Nuovo")
    assert body["message_template_c"].startswith("Template C")


def test_update_message_template_c_removed_with_explicit_none_while_running(client, _temp_db):
    """'None esplicito = rimuovi' (semantica Task 1, gia' usata da message_template_b)
    deve funzionare end-to-end anche per message_template_c attraverso l'endpoint,
    non solo a livello schema — e anche questo mentre la campagna e' running."""
    _, sf = _temp_db
    camp = _make_campaign(name="run-remove-c", status=CampaignStatus.running)
    camp.message_template_c = "Template C da rimuovere abbastanza lungo"
    _seed(sf, camp)

    resp = client.put(f"/api/campaigns/{camp.id}", json={"message_template_c": None})
    assert resp.status_code == 200, resp.text
    assert resp.json()["message_template_c"] is None


def test_update_template_b_and_prompt_context_while_running(client, _temp_db):
    """message_template_b e ai_prompt_context erano gia' setter esistenti (Task 1) ma
    passavano dal gate esterno: verifica che ora siano davvero raggiungibili a running,
    non solo message_template_c/ai_enabled (che hanno setter NUOVI di questo task)."""
    _, sf = _temp_db
    camp = _make_campaign(name="run-b-ctx", status=CampaignStatus.running)
    _seed(sf, camp)

    resp = client.put(f"/api/campaigns/{camp.id}", json={
        "message_template_b": "Template B abbastanza lungo per la variante",
        "ai_prompt_context": "Contesto AI aggiornato per la campagna",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["message_template_b"].startswith("Template B")
    assert body["ai_prompt_context"].startswith("Contesto AI")


@pytest.mark.parametrize("blocked_status", [
    CampaignStatus.scraping,
    CampaignStatus.listing,
    CampaignStatus.scraping_and_running,
    CampaignStatus.error,
    CampaignStatus.completed,
])
def test_update_message_fields_in_every_status(client, _temp_db, blocked_status):
    """'in QUALSIASI stato' per davvero: non solo running, anche gli altri stati che
    PRIMA di questo task erano bloccati dal gate esterno (scraping/listing/error/
    completed) devono ora accettare l'update dei campi messaggi/AI."""
    _, sf = _temp_db
    camp = _make_campaign(name=f"any-{blocked_status.value}", status=blocked_status)
    _seed(sf, camp)

    resp = client.put(f"/api/campaigns/{camp.id}", json={
        "base_message_template": f"Template per stato {blocked_status.value} lungo",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["base_message_template"].startswith("Template per stato")


def test_name_update_still_blocked_while_running(client, _temp_db):
    """Boundary check: SOLO i campi messaggi/AI promossi diventano always-editable.
    'name' non e' tra questi e deve restare bloccato a running — la promozione non
    deve aver allargato il gate a tutto il payload."""
    _, sf = _temp_db
    camp = _make_campaign(name="run-name-guard", status=CampaignStatus.running)
    _seed(sf, camp)

    resp = client.put(f"/api/campaigns/{camp.id}", json={"name": "Nome nuovo"})
    assert resp.status_code == 400, resp.text


def test_bio_engine_still_blocked_while_running(client, _temp_db):
    _, sf = _temp_db
    camp = _make_campaign(name="run2", status=CampaignStatus.running, bio_engine="api")
    _seed(sf, camp)

    resp = client.put(f"/api/campaigns/{camp.id}", json={"bio_engine": "browser"})
    assert resp.status_code == 400, resp.text
