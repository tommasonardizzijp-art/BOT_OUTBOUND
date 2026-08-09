"""Il gate sulla combinazione dei tre motori.

Il vincolo non e' su bio_engine soltanto: la Fase Bio e' governata da
enrichment_level, che e' ortogonale, e' controllato PRIMA di bio_engine
(scrape_bios.py:82 contro :114) e vale 'none' di default sulle campagne nuove.
"""
import pytest

from app.services.inbox_browser.gate import valida_combinazione_motori


def test_combinazione_valida():
    assert valida_combinazione_motori("browser", "browser", "contacts") is None
    assert valida_combinazione_motori("browser", "browser", "bio") is None


def test_inbox_api_non_e_vincolato():
    """Il motore API resta libero: nessuna regressione sul percorso esistente."""
    assert valida_combinazione_motori("api", "api", "none") is None
    assert valida_combinazione_motori("api", "browser", "none") is None


def test_browser_con_arricchimento_none_e_rifiutato():
    """Il buco trovato in revisione: e' la configurazione DI DEFAULT."""
    msg = valida_combinazione_motori("browser", "browser", "none")
    assert msg is not None
    assert "arricchimento" in msg.lower()


def test_browser_con_bio_engine_api_e_rifiutato():
    msg = valida_combinazione_motori("browser", "api", "contacts")
    assert msg is not None
    assert "browser" in msg.lower()


def test_entrambi_gli_errori_insieme_producono_un_messaggio():
    assert valida_combinazione_motori("browser", "api", "none") is not None


@pytest.mark.parametrize("livello", ["bio", "contacts"])
def test_tutti_i_livelli_non_none_sono_ammessi(livello):
    assert valida_combinazione_motori("browser", "browser", livello) is None


# ============================================================================
# PARTE B — integrazione sull'endpoint (mirror del pattern in
# test_inbox_engine_switch_adversarial.py: TestClient sincrono + SQLite temp
# module-scoped).
# ============================================================================

import asyncio
import os
import tempfile
import uuid
from datetime import datetime

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


@pytest.fixture(scope="module")
def _temp_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="e2e_inbox_browser_gate_")
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


def _seed_campagna_api_none(session_factory) -> str:
    """Campagna inbox='api', bio='api', arricchimento='none' — stato di partenza
    non pericoloso finche' resta su API, ma e' il caso che il gate scritto male
    lascerebbe passare con un PATCH singolo su inbox_engine."""
    camp_id = str(uuid.uuid4())

    async def _seed(db):
        camp = Campaign(
            id=camp_id,
            name="gate-campagna-api-none",
            source_type="scrape",
            target_username="target_user",
            scrape_mode="followers",
            status=CampaignStatus.draft,
            inbox_engine="api",
            bio_engine="api",
            enrichment_level="none",
            messaging_enabled=False,
        )
        db.add(camp)
        await db.commit()

    _run(session_factory, _seed)
    return camp_id


def test_PATCH_singolo_non_aggira_il_gate(client, _temp_db):
    """Il caso che un gate scritto male lascerebbe passare.

    Campagna con inbox='api', bio='api', arricchimento='none'. Un solo PATCH che
    cambia il motore inbox deve essere RIFIUTATO: da solo produrrebbe una
    combinazione incoerente (inbox='browser', bio='api', arricchimento='none').
    """
    _, sf = _temp_db
    camp_id = _seed_campagna_api_none(sf)

    resp = client.put(
        f"/api/campaigns/{camp_id}", json={"inbox_engine": "browser"},
    )
    assert resp.status_code == 400, f"combinazione incoerente accettata: {resp.text}"


def test_PATCH_completo_e_accettato(client, _temp_db):
    _, sf = _temp_db
    camp_id = _seed_campagna_api_none(sf)

    resp = client.put(
        f"/api/campaigns/{camp_id}",
        json={"inbox_engine": "browser", "bio_engine": "browser", "enrichment_level": "contacts"},
    )
    assert resp.status_code == 200, resp.text
