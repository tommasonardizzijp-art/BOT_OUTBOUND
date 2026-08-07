"""Il livello si imposta alla creazione e si cambia dopo (upgrade/downgrade).

Due strati:
  - schema puro (CampaignCreate/CampaignUpdate): pattern, default, "omesso=invariato";
  - end-to-end via API (mirrors test_bio_engine_api.py): create persiste il livello,
    update lo guarda allo stesso modo di bio_engine (draft/ready/paused/error), la
    campagna in corso lo rifiuta.
"""
import asyncio
import os
import tempfile
import uuid
from datetime import datetime

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.schemas.campaign import CampaignCreate, CampaignUpdate

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


def _base(**kw):
    d = dict(name="c", target_username="tizio",
             base_message_template="ciao come va tutto bene")
    d.update(kw)
    return d


def test_default_none_alla_creazione():
    assert CampaignCreate(**_base()).enrichment_level == "none"


@pytest.mark.parametrize("livello", ["none", "bio", "contacts"])
def test_accetta_i_tre_livelli(livello):
    assert CampaignCreate(**_base(enrichment_level=livello)).enrichment_level == livello


@pytest.mark.parametrize("livello", ["nessuno", "BIO", "contact", "", "api"])
def test_rifiuta_i_livelli_inventati(livello):
    with pytest.raises(ValidationError):
        CampaignCreate(**_base(enrichment_level=livello))


def test_update_puo_alzare_e_abbassare_il_livello():
    assert CampaignUpdate(enrichment_level="contacts").enrichment_level == "contacts"
    assert CampaignUpdate(enrichment_level="none").enrichment_level == "none"
    # Omesso = invariato
    assert CampaignUpdate(name="x").enrichment_level is None


# ---------- validazione: casi ostili oltre quelli del brief -------------------

@pytest.mark.parametrize("livello", [
    "contacts ",  # spazio finale
    "none; DROP TABLE campaigns",
    "x" * 10_000,
])
def test_rifiuta_livelli_ostili(livello):
    with pytest.raises(ValidationError):
        CampaignCreate(**_base(enrichment_level=livello))


def test_rifiuta_none_esplicito_alla_creazione():
    """CampaignCreate.enrichment_level e' str (non opzionale): un null esplicito
    nel payload non deve essere silenziosamente normalizzato a 'none'."""
    with pytest.raises(ValidationError):
        CampaignCreate(**_base(enrichment_level=None))


@pytest.mark.parametrize("livello", [123, 1.5, ["bio"], {"v": "bio"}])
def test_rifiuta_tipi_sbagliati(livello):
    with pytest.raises(ValidationError):
        CampaignCreate(**_base(enrichment_level=livello))


# ---------- end-to-end via API --------------------------------------------------
# Mirrors test_bio_engine_api.py: module-scoped temp SQLite, dependency_overrides
# per get_db + get_current_user, TestClient sulle route vere.

@pytest.fixture(scope="module")
def _temp_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="e2e_enrichment_level_")
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
            id="00000000-0000-0000-0000-000000000004",
            email="admin3@test.local",
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


def _make_campaign(*, name: str, status: CampaignStatus, enrichment_level: str = "none") -> Campaign:
    return Campaign(
        id=str(uuid.uuid4()),
        name=name,
        source_type="scrape",
        target_username="target_user",
        scrape_mode="followers",
        enrichment_level=enrichment_level,
        status=status,
        messaging_enabled=False,
    )


def test_create_campaign_persists_enrichment_level(client):
    resp = client.post(
        "/api/campaigns",
        json={
            "name": "Enrichment Create Test",
            "source_type": "scrape",
            "target_username": "some_target",
            "messaging_enabled": False,
            "enrichment_level": "contacts",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["enrichment_level"] == "contacts"


def test_create_campaign_defaults_enrichment_level_to_none(client):
    resp = client.post(
        "/api/campaigns",
        json={
            "name": "Enrichment Default Test",
            "source_type": "scrape",
            "target_username": "some_target",
            "messaging_enabled": False,
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["enrichment_level"] == "none"


def test_create_campaign_rejects_invalid_enrichment_level(client):
    resp = client.post(
        "/api/campaigns",
        json={
            "name": "Enrichment Invalid Create Test",
            "source_type": "scrape",
            "target_username": "some_target",
            "messaging_enabled": False,
            "enrichment_level": "BIO",
        },
    )
    assert resp.status_code == 422, resp.text


def test_update_enrichment_level_allowed_in_draft(client, _temp_db):
    _, sf = _temp_db
    camp = _make_campaign(name="Draft Enrichment Switch", status=CampaignStatus.draft)
    camp_id = camp.id

    async def _seed(db):
        db.add(camp)
        await db.commit()

    _run(sf, _seed)

    resp = client.put(f"/api/campaigns/{camp_id}", json={"enrichment_level": "bio"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["enrichment_level"] == "bio"


@pytest.mark.parametrize("allowed_status", [
    CampaignStatus.ready,
    CampaignStatus.paused,
    CampaignStatus.error,
])
def test_update_enrichment_level_allowed_when_stopped(client, _temp_db, allowed_status):
    _, sf = _temp_db
    camp = _make_campaign(name=f"Allowed-{allowed_status.value}", status=allowed_status)
    camp_id = camp.id

    async def _seed(db):
        db.add(camp)
        await db.commit()

    _run(sf, _seed)

    resp = client.put(f"/api/campaigns/{camp_id}", json={"enrichment_level": "contacts"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["enrichment_level"] == "contacts"


@pytest.mark.parametrize("blocked_status", [
    CampaignStatus.scraping,
    CampaignStatus.scraping_break,
    CampaignStatus.running,
    CampaignStatus.scraping_and_running,
    CampaignStatus.listing,
    CampaignStatus.completed,
])
def test_update_enrichment_level_rejected_while_running(client, _temp_db, blocked_status):
    """Il livello decide SE si aprono i profili: cambiarlo mentre la Fase Bio gira
    lascerebbe i worker a meta' con un'assunzione non piu' valida (stesso rischio
    documentato per bio_engine). Rifiutato con 400 negli stati IN CORSO."""
    _, sf = _temp_db
    camp = _make_campaign(name=f"Blocked-{blocked_status.value}", status=blocked_status)
    camp_id = camp.id

    async def _seed(db):
        db.add(camp)
        await db.commit()

    _run(sf, _seed)

    resp = client.put(f"/api/campaigns/{camp_id}", json={"enrichment_level": "bio"})
    assert resp.status_code == 400, (
        f"DEFECT: enrichment_level switch allowed in status={blocked_status.value!r}. "
        f"Got {resp.status_code}: {resp.text}"
    )
    detail = resp.json().get("detail", "")
    assert any(kw in detail.lower() for kw in ("arricchimento", "draft", "ferma")), (
        f"400 came from an unexpected guard (not the enrichment_level guard): {detail}"
    )


def test_update_enrichment_level_rejects_invalid_value(client, _temp_db):
    _, sf = _temp_db
    camp = _make_campaign(name="Invalid Update Test", status=CampaignStatus.draft)
    camp_id = camp.id

    async def _seed(db):
        db.add(camp)
        await db.commit()

    _run(sf, _seed)

    resp = client.put(f"/api/campaigns/{camp_id}", json={"enrichment_level": "nope"})
    assert resp.status_code == 422, resp.text


def test_update_without_enrichment_level_does_not_reset_it(client, _temp_db):
    """PATCH parziale: un update che non manda enrichment_level non deve azzerarlo
    (tornare a 'none') ne' toccarlo in alcun modo."""
    _, sf = _temp_db
    camp = _make_campaign(name="No Clobber Enrichment", status=CampaignStatus.draft, enrichment_level="contacts")
    camp_id = camp.id

    async def _seed(db):
        db.add(camp)
        await db.commit()

    _run(sf, _seed)

    resp = client.put(f"/api/campaigns/{camp_id}", json={"name": "renamed"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enrichment_level"] == "contacts"
    assert body["name"] == "renamed"
