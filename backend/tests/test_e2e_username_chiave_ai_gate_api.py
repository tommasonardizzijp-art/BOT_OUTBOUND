"""E2E — la guardia AI-senza-bio (`valida_ai_senza_bio`, app/models/campaign.py)
attraverso HTTP: POST /api/campaigns e PUT /api/campaigns/{id}, su una campagna
che vive per tutto il test (creata, poi modificata).

Copiato dall'harness autenticata di test_enrichment_level_api.py:92-151 (stesso
pattern: TestClient sulle route vere, dependency_overrides su get_db/get_current_user,
DB SQLite temporaneo module-scoped). Id utente 000...0006 — 0004 e 0005 sono gia'
presi da altri moduli di test che condividono lo stesso processo pytest.
"""
import asyncio
import os
import tempfile
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
from app.models.user import User
from app.utils.auth_deps import get_current_user


@pytest.fixture(scope="module")
def _temp_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="e2e_ai_gate_")
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
            id="00000000-0000-0000-0000-000000000006",
            email="admin-aigate@test.local",
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


def _payload(**kw):
    d = dict(
        name="AI gate e2e",
        source_type="scrape",
        target_username="target_ai_gate",
        messaging_enabled=False,
    )
    d.update(kw)
    return d


# ── Creazione: entrambe le direzioni bloccate/permesse ──────────────────────

def test_create_ai_enabled_su_livello_none_e_bloccata(client):
    resp = client.post("/api/campaigns", json=_payload(ai_enabled=True, enrichment_level="none"))
    assert resp.status_code == 400, resp.text
    assert "bio" in resp.json()["detail"].lower()


def test_create_ai_enabled_su_livello_bio_e_permessa(client):
    resp = client.post("/api/campaigns", json=_payload(
        name="AI gate e2e — valida", ai_enabled=True, enrichment_level="bio",
    ))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["ai_enabled"] is True
    assert body["enrichment_level"] == "bio"


def test_create_ai_spenta_su_livello_none_e_permessa(client):
    resp = client.post("/api/campaigns", json=_payload(
        name="AI gate e2e — template", ai_enabled=False, enrichment_level="none",
    ))
    assert resp.status_code == 201, resp.text


# ── Campagna viva: creata, poi modificata via PUT in entrambe le direzioni ──

def test_put_alza_ai_su_campagna_gia_a_livello_none_e_bloccato(client):
    """Campagna creata valida (AI spenta, livello none). PUT che accende l'AI
    senza toccare il livello: deve essere rifiutato — stessa combinazione finale
    vietata alla creazione."""
    creato = client.post("/api/campaigns", json=_payload(
        name="AI gate e2e — put direzione 1", ai_enabled=False, enrichment_level="none",
    ))
    assert creato.status_code == 201, creato.text
    campaign_id = creato.json()["id"]

    resp = client.put(f"/api/campaigns/{campaign_id}", json={"ai_enabled": True})
    assert resp.status_code == 400, resp.text
    assert "bio" in resp.json()["detail"].lower()

    # La campagna non deve essere rimasta modificata a meta'.
    rileggi = client.get(f"/api/campaigns/{campaign_id}")
    assert rileggi.json()["ai_enabled"] is False, "l'AI e' rimasta accesa nonostante il 400"


def test_put_abbassa_livello_su_campagna_con_ai_accesa_e_bloccato(client):
    """Campagna creata valida (AI accesa, livello bio). PUT che abbassa il
    livello a none senza toccare l'AI: deve essere rifiutato."""
    creato = client.post("/api/campaigns", json=_payload(
        name="AI gate e2e — put direzione 2", ai_enabled=True, enrichment_level="bio",
    ))
    assert creato.status_code == 201, creato.text
    campaign_id = creato.json()["id"]

    resp = client.put(f"/api/campaigns/{campaign_id}", json={"enrichment_level": "none"})
    assert resp.status_code == 400, resp.text
    assert "bio" in resp.json()["detail"].lower()

    rileggi = client.get(f"/api/campaigns/{campaign_id}")
    assert rileggi.json()["enrichment_level"] == "bio", "il livello e' sceso nonostante il 400"


def test_put_verso_una_combinazione_valida_non_ha_intralci(client):
    """Campagna creata valida (AI spenta, livello bio). PUT che accende l'AI:
    resta valida (bio c'e' gia'), deve passare senza toccare il gate."""
    creato = client.post("/api/campaigns", json=_payload(
        name="AI gate e2e — put valido", ai_enabled=False, enrichment_level="bio",
    ))
    assert creato.status_code == 201, creato.text
    campaign_id = creato.json()["id"]

    resp = client.put(f"/api/campaigns/{campaign_id}", json={"ai_enabled": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["ai_enabled"] is True
    assert resp.json()["enrichment_level"] == "bio"

    # E il giro inverso: si puo' spegnere l'AI restando a bio senza intralci.
    resp2 = client.put(f"/api/campaigns/{campaign_id}", json={"ai_enabled": False})
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["ai_enabled"] is False
