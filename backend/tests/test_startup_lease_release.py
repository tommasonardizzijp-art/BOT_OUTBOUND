"""Cold-start guard: rilascia i lease account ORFANI al restart.

Bug reale: dopo un restart del backend, un worker ucciso lascia il suo lease sul
DB con expiry ancora nel futuro (TTL 15 min). Il nuovo worker (owner diverso) non
puo' acquisirlo -> "already leased by another job, exiting" anche con i browser
tutti chiusi. La guardia di startup ora azzera questi lead orfani.
"""
import asyncio
import os
import tempfile
import uuid
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.account import InstagramAccount, AccountStatus


def _setup_db(monkeypatch):
    from app.database import Base
    import app.services.work_enqueue as we

    fd, path = tempfile.mkstemp(suffix=".db", prefix="lease_")
    os.close(fd)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{path}", connect_args={"check_same_thread": False}
    )
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_seed())
    monkeypatch.setattr(we, "AsyncSessionLocal", session_factory)

    def cleanup():
        asyncio.run(engine.dispose())
        try:
            os.remove(path)
        except OSError:
            pass

    return session_factory, cleanup


def _add_account(session_factory, *, lease_owner, lease_expires_at):
    acc_id = str(uuid.uuid4())

    async def _go():
        async with session_factory() as db:
            db.add(InstagramAccount(
                id=acc_id, username=f"acc_{acc_id[:6]}",
                encrypted_password="x", status=AccountStatus.active,
                daily_message_limit=20,
                lease_owner=lease_owner, lease_expires_at=lease_expires_at,
            ))
            await db.commit()

    asyncio.run(_go())
    return acc_id


def _lease_of(session_factory, acc_id):
    async def _go():
        async with session_factory() as db:
            a = await db.get(InstagramAccount, acc_id)
            return a.lease_owner, a.lease_expires_at

    return asyncio.run(_go())


def test_cold_start_rilascia_lease_orfano_non_scaduto(monkeypatch):
    """Lease con owner morto ed expiry NEL FUTURO (worker ucciso dal restart): la
    guardia lo rilascia -> il nuovo worker parte subito invece di aspettare 15 min."""
    import app.services.work_enqueue as we
    session_factory, cleanup = _setup_db(monkeypatch)
    try:
        future = datetime.utcnow() + timedelta(minutes=12)  # NON scaduto: acquire non lo recupererebbe
        acc_id = _add_account(session_factory, lease_owner="worker:dead:xyz", lease_expires_at=future)

        counts = asyncio.run(we.pause_active_work_on_startup())

        owner, exp = _lease_of(session_factory, acc_id)
        assert owner is None, f"lease non rilasciato: {owner!r}"
        assert exp is None
        assert counts["leases_released"] == 1
    finally:
        cleanup()


def test_cold_start_niente_da_rilasciare(monkeypatch):
    """Account senza lease: la guardia non tocca nulla, leases_released=0."""
    import app.services.work_enqueue as we
    session_factory, cleanup = _setup_db(monkeypatch)
    try:
        acc_id = _add_account(session_factory, lease_owner=None, lease_expires_at=None)
        counts = asyncio.run(we.pause_active_work_on_startup())
        owner, exp = _lease_of(session_factory, acc_id)
        assert owner is None and exp is None
        assert counts["leases_released"] == 0
    finally:
        cleanup()
