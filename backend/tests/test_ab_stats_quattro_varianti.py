"""ab-stats con quattro template: A/B/C/D contate, caselle sempre presenti.

Prima l'endpoint mappava solo `variant_a`/`variant_b`: con quattro template attivi
(migration 024) i messaggi generati con C e D finivano nel nulla e la UI mostrava
un A/B test falsato. Qui si verifica che ogni variante abbia la sua riga, che le
chiavi esistano anche quando la variante non e' mai stata usata (casella vuota, non
casella che sparisce) e che i messaggi storici senza variante si sommino alla A.

Pattern fixture: come test_bio_engine_api.py (SQLite temporaneo + TestClient).
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
import app.models.global_contact  # noqa: F401
import app.models.imported_profile  # noqa: F401
import app.models.user  # noqa: F401

from app.database import Base, get_db
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.models.message import Message, MessageStatus
from app.models.user import User
from app.utils.auth_deps import get_current_user


CAMPAIGN_ID = str(uuid.uuid4())


@pytest.fixture(scope="module")
def _temp_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="ab_stats_")
    os.close(fd)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{path}", connect_args={"check_same_thread": False}
    )
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    yield engine, session_factory

    asyncio.run(engine.dispose())
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
            id="00000000-0000-0000-0000-000000000009",
            email="admin_ab@test.local",
            password_hash="x",
            role="admin",
            is_active=True,
            created_at=datetime.utcnow(),
        )

    from app.main import app
    from fastapi.testclient import TestClient

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _override_get_current_user
    c = TestClient(app, raise_server_exceptions=True)
    yield c
    app.dependency_overrides.clear()


def _seed(session_factory, righe):
    """righe = [(variante, stato_messaggio, ha_risposto)]"""
    async def _go():
        async with session_factory() as db:
            db.add(Campaign(
                id=CAMPAIGN_ID, name="AB quattro", source_type="scrape",
                status=CampaignStatus.running, messaging_enabled=True,
                base_message_template="ciao {nome}",
            ))
            for i, (variante, stato, ha_risposto) in enumerate(righe):
                fid = str(uuid.uuid4())
                db.add(Follower(
                    id=fid, campaign_id=CAMPAIGN_ID,
                    ig_user_id=1000 + i, username=f"lead{i}",
                    status=FollowerStatus.replied if ha_risposto else FollowerStatus.sent,
                ))
                db.add(Message(
                    id=str(uuid.uuid4()), campaign_id=CAMPAIGN_ID, follower_id=fid,
                    generated_text="x", status=stato, template_variant=variante,
                ))
            await db.commit()

    asyncio.run(_go())


def test_ab_stats_conta_tutte_e_quattro_le_varianti(client, _temp_db):
    _, session_factory = _temp_db
    _seed(session_factory, [
        ("a", MessageStatus.sent, True),
        ("a", MessageStatus.sent, False),
        ("b", MessageStatus.sent, False),
        ("c", MessageStatus.sent, True),
        ("c", MessageStatus.failed, False),
        ("d", MessageStatus.pending, False),
        (None, MessageStatus.sent, False),   # storico pre-A/B: si somma alla A
    ])

    resp = client.get(f"/api/campaigns/{CAMPAIGN_ID}/ab-stats")
    assert resp.status_code == 200
    body = resp.json()

    # Le quattro chiavi ci sono sempre: la UI tiene quattro caselle fisse.
    for key in ("variant_a", "variant_b", "variant_c", "variant_d"):
        assert key in body

    # A = 2 righe 'a' + 1 riga senza variante (somma, non sovrascrittura)
    assert body["variant_a"]["sent"] == 3
    assert body["variant_a"]["replied"] == 1
    assert body["variant_a"]["reply_rate"] == pytest.approx(1 / 3)

    assert body["variant_b"]["sent"] == 1
    assert body["variant_b"]["replied"] == 0

    assert body["variant_c"]["sent"] == 1
    assert body["variant_c"]["failed"] == 1
    assert body["variant_c"]["reply_rate"] == pytest.approx(1.0)

    assert body["variant_d"]["pending"] == 1
    assert body["variant_d"]["sent"] == 0
    assert body["variant_d"]["reply_rate"] == 0.0

    assert body["template_b_present"] is True
    assert body["template_c_present"] is True
    assert body["template_d_present"] is True


def test_ab_stats_varianti_mai_usate_restano_chiavi_vuote(client, _temp_db):
    """Campagna con il solo template A: B/C/D tornano None (casella vuota in UI)."""
    _, session_factory = _temp_db
    solo_a = str(uuid.uuid4())

    async def _go():
        async with session_factory() as db:
            db.add(Campaign(
                id=solo_a, name="Solo A", source_type="scrape",
                status=CampaignStatus.running, messaging_enabled=True,
                base_message_template="ciao {nome}",
            ))
            fid = str(uuid.uuid4())
            db.add(Follower(
                id=fid, campaign_id=solo_a, ig_user_id=9001,
                username="lead_solo_a", status=FollowerStatus.sent,
            ))
            db.add(Message(
                id=str(uuid.uuid4()), campaign_id=solo_a, follower_id=fid,
                generated_text="x", status=MessageStatus.sent, template_variant="a",
            ))
            await db.commit()

    asyncio.run(_go())

    body = client.get(f"/api/campaigns/{solo_a}/ab-stats").json()
    assert body["variant_a"]["sent"] == 1
    assert body["variant_b"] is None
    assert body["variant_c"] is None
    assert body["variant_d"] is None
    assert body["template_b_present"] is False
    assert body["template_c_present"] is False
    assert body["template_d_present"] is False
