"""E2E — dm_harvest.harvest_profile_into_follower contro un DB SQLite VERO, non
un doppio finto. Una AsyncSession vera, un Follower vero con targa provvisoria
sotto UniqueConstraint(campaign_id, ig_user_id) (app/models/follower.py:24),
e la verifica che dopo l'harvest la riga porti il pk reale.

Copre anche il caso in cui un'ALTRA riga della stessa campagna ha gia' quel pk:
deve non fondere, non sollevare, e segnalare skip_reason.
"""
import asyncio
import os
import tempfile
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models.account  # noqa: F401
import app.models.activity_log  # noqa: F401
import app.models.campaign_account  # noqa: F401
import app.models.global_contact  # noqa: F401
import app.models.imported_profile  # noqa: F401
import app.models.message  # noqa: F401
import app.models.user  # noqa: F401

from app.database import Base
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.services.dm_harvest import harvest_profile_into_follower


@pytest.fixture()
def db_session():
    """DB SQLite temporaneo vero, una sessione per test. File a parte, non lo
    slot condiviso di conftest: qui serve un impianto minimo isolato per test,
    non l'app intera."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="e2e_dm_harvest_")
    os.close(fd)
    url = f"sqlite+aiosqlite:///{path}"
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())

    yield session_factory

    async def _dispose():
        await engine.dispose()

    asyncio.run(_dispose())
    try:
        os.remove(path)
    except OSError:
        pass


def _payload_graphql(pk: int, username: str = "mario_shop", **kw) -> dict:
    """Forma GraphQL grezza (PolarisProfilePageContentQuery), come la cattura
    l'invio (dm_harvest la passa a graphql_user_to_web_shape prima dello shim)."""
    d = dict(pk=pk, username=username, full_name="Mario Shop", biography="")
    d.update(kw)
    return d


def _crea_campagna_e_follower(session_factory, *, ig_user_id, username, campaign_id=None):
    campaign_id = campaign_id or str(uuid.uuid4())

    async def _go():
        async with session_factory() as db:
            existing = await db.get(Campaign, campaign_id)
            if existing is None:
                db.add(Campaign(
                    id=campaign_id, name="dm harvest e2e", source_type="scrape",
                    target_username="target", status=CampaignStatus.running,
                    messaging_enabled=True,
                ))
            follower_id = str(uuid.uuid4())
            db.add(Follower(
                id=follower_id, campaign_id=campaign_id, ig_user_id=ig_user_id,
                username=username, status=FollowerStatus.sent, source_channel="browser",
            ))
            await db.commit()
            return follower_id
    fid = asyncio.run(_go())
    return campaign_id, fid


def _rileggi(session_factory, follower_id):
    async def _go():
        async with session_factory() as db:
            return await db.get(Follower, follower_id)
    return asyncio.run(_go())


def _rileggi_tutti(session_factory, campaign_id):
    async def _go():
        async with session_factory() as db:
            return (await db.execute(
                select(Follower).where(Follower.campaign_id == campaign_id)
            )).scalars().all()
    return asyncio.run(_go())


def test_ancora_la_targa_provvisoria_al_pk_vero(db_session):
    campaign_id, follower_id = _crea_campagna_e_follower(
        db_session, ig_user_id=-8347, username="mario_shop",
    )

    async def _go():
        async with db_session() as db:
            follower = await db.get(Follower, follower_id)
            ok = await harvest_profile_into_follower(db, follower, _payload_graphql(555))
            return ok
    scritto = asyncio.run(_go())

    assert scritto is True
    follower = _rileggi(db_session, follower_id)
    assert follower.ig_user_id == 555, f"targa non ancorata: resta {follower.ig_user_id}"
    assert follower.full_name == "Mario Shop"


def test_pk_gia_presente_su_altra_riga_non_fonde_e_non_solleva(db_session):
    """Altra riga della stessa campagna ha gia' il pk 555. L'harvest sulla riga
    a targa provvisoria NON deve fonderla ne' sollevare: skip con motivazione."""
    campaign_id, follower_provvisorio = _crea_campagna_e_follower(
        db_session, ig_user_id=-8347, username="mario_shop",
    )
    # Seconda riga della stessa campagna, gia' col pk vero.
    async def _semina_bersaglio():
        async with db_session() as db:
            db.add(Follower(
                campaign_id=campaign_id, ig_user_id=555, username="mario_shop_dup",
                status=FollowerStatus.pending,
            ))
            await db.commit()
    asyncio.run(_semina_bersaglio())

    async def _go():
        async with db_session() as db:
            follower = await db.get(Follower, follower_provvisorio)
            ok = await harvest_profile_into_follower(db, follower, _payload_graphql(555))
            return ok
    scritto = asyncio.run(_go())

    assert scritto is False, "non doveva scrivere: il pk e' gia' su un'altra riga"
    righe = _rileggi_tutti(db_session, campaign_id)
    assert len(righe) == 2, f"non deve fondere le righe: {len(righe)}"
    targhe = sorted(r.ig_user_id for r in righe)
    assert targhe == [-8347, 555], (
        f"la riga provvisoria deve restare provvisoria, non essere promossa: {targhe}"
    )
    provvisoria = next(r for r in righe if r.ig_user_id == -8347)
    assert provvisoria.skip_reason == "targa_gia_presente_su_altra_riga"


def test_identita_cambiata_su_pk_reale_diverso_non_sovrascrive(db_session):
    """La riga ha gia' un pk VERO (non provvisorio) e il payload ne porta uno
    diverso: username riassegnato dopo un rename. Non deve scrivere sulla scheda
    sbagliata, deve marcare skip_reason='handle_riassegnato'."""
    campaign_id, follower_id = _crea_campagna_e_follower(
        db_session, ig_user_id=111, username="negozio",
    )

    async def _go():
        async with db_session() as db:
            follower = await db.get(Follower, follower_id)
            ok = await harvest_profile_into_follower(db, follower, _payload_graphql(222, username="negozio"))
            return ok
    scritto = asyncio.run(_go())

    assert scritto is False
    follower = _rileggi(db_session, follower_id)
    assert follower.ig_user_id == 111, "il pk non doveva cambiare"
    assert follower.skip_reason == "handle_riassegnato"
