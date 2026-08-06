"""Completamento campagna: i lead in attesa di Fase Bio non si buttano via.

Bug reale (FLASH LASER, 06/08): 241 lead, 126 con messaggio pronto e 102 ancora
`pending` (Fase Bio mai finita). Finiti i 126, il worker chiamava
`_maybe_complete_campaign`, che conta come "rimanenti" solo
bio_scraped/message_generated/pending_approval/locked: i 102 `pending` non
comparivano, la campagna passava a `completed` e quei lead sparivano dal giro.

Ora: se restano lead senza bio la campagna NON e' completata; va in pausa con un
motivo esplicito, perche' serve lanciare la Fase Bio.
"""
import asyncio
import os
import tempfile
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.services.campaign_orchestrator import _maybe_complete_campaign


@pytest.fixture
def session_factory():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="complete_bio_")
    os.close(fd)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{path}", connect_args={"check_same_thread": False}
    )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    yield factory
    asyncio.run(engine.dispose())
    try:
        os.remove(path)
    except OSError:
        pass


async def _seed(factory, campaign_id, stati):
    async with factory() as db:
        db.add(Campaign(
            id=campaign_id, name="completamento", source_type="scrape",
            status=CampaignStatus.running, messaging_enabled=True,
            base_message_template="ciao {nome}",
        ))
        for i, stato in enumerate(stati):
            db.add(Follower(
                id=str(uuid.uuid4()), campaign_id=campaign_id,
                ig_user_id=5000 + i, username=f"lead{i}", status=stato,
            ))
        await db.commit()


async def _status(factory, campaign_id):
    async with factory() as db:
        c = await db.get(Campaign, campaign_id)
        return c.status


@pytest.mark.asyncio
async def test_non_completa_se_restano_lead_in_attesa_di_bio(session_factory):
    cid = str(uuid.uuid4())
    await _seed(session_factory, cid, [FollowerStatus.sent] * 3 + [FollowerStatus.pending] * 4)

    async with session_factory() as db:
        await _maybe_complete_campaign(cid, db)

    assert await _status(session_factory, cid) == CampaignStatus.paused, (
        "campagna chiusa con lead ancora senza bio"
    )


@pytest.mark.asyncio
async def test_completa_quando_non_resta_davvero_nulla(session_factory):
    cid = str(uuid.uuid4())
    await _seed(
        session_factory, cid,
        [FollowerStatus.sent] * 3 + [FollowerStatus.skipped, FollowerStatus.failed],
    )

    async with session_factory() as db:
        await _maybe_complete_campaign(cid, db)

    assert await _status(session_factory, cid) == CampaignStatus.completed


@pytest.mark.asyncio
async def test_lead_pronti_ancora_in_coda_non_chiudono_ne_pausano(session_factory):
    """Guardia di regressione: con lead pronti al DM non si tocca lo stato."""
    cid = str(uuid.uuid4())
    await _seed(
        session_factory, cid,
        [FollowerStatus.sent, FollowerStatus.message_generated, FollowerStatus.pending],
    )

    async with session_factory() as db:
        await _maybe_complete_campaign(cid, db)

    assert await _status(session_factory, cid) == CampaignStatus.running
