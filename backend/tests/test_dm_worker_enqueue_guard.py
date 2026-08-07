"""Bug reale (03/08): un resume (/start, /unhalt telegram, admin resume-all)
chiamava `_enqueue_dm_workers_with_redis`, che cancellava incondizionatamente
arq:job/retry/in-progress per il worker e ne accodava uno nuovo — anche quando
il vecchio job era ancora vivo (in esecuzione o in pausa-sessione con un
Retry(defer=...) parcheggiato). Il job parcheggiato spariva nel nulla; quello
nuovo falliva subito su `account_lease.acquire()` (lease Postgres ancora
valida, indipendente da Redis) e non restava piu' nulla in coda: la campagna
restava a status=running senza worker per sempre.

La guardia ora salta l'enqueue se una qualunque delle tre chiavi ARQ del
job_id esiste gia' (stesso pattern di `_reenqueue_phase` per scrape/list/bios).
"""
import asyncio
import os
import tempfile
import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.account import InstagramAccount, AccountStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.campaign_account import CampaignAccount
import app.services.work_enqueue as we


def _setup_db(monkeypatch):
    from app.database import Base

    fd, path = tempfile.mkstemp(suffix=".db", prefix="enqueue_guard_")
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


def _seed_campaign_with_account(session_factory) -> tuple[str, str]:
    campaign_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())

    async def _go():
        async with session_factory() as db:
            db.add(Campaign(
                id=campaign_id, name="Test", target_username="target",
                base_message_template="Messaggio abbastanza lungo",
                status=CampaignStatus.running,
            ))
            db.add(InstagramAccount(
                id=account_id, username="acct1", encrypted_password="x",
                status=AccountStatus.active, daily_message_limit=20,
            ))
            db.add(CampaignAccount(
                campaign_id=campaign_id, account_id=account_id,
                is_active=True, role="both",
            ))
            await db.commit()

    asyncio.run(_go())
    return campaign_id, account_id


class FakeRedis:
    """Stub minimale: registra le chiamate, simula chiavi ARQ preesistenti."""

    def __init__(self, existing_keys: set[str] = frozenset()):
        self.existing_keys = set(existing_keys)
        self.deleted: list[str] = []
        self.enqueued: list[tuple] = []

    async def exists(self, key: str) -> bool:
        return key in self.existing_keys

    async def delete(self, *keys: str) -> None:
        self.deleted.extend(keys)

    async def enqueue_job(self, *args, **kwargs) -> None:
        self.enqueued.append((args, kwargs))


def test_enqueue_skips_when_job_already_in_progress(monkeypatch):
    session_factory, cleanup = _setup_db(monkeypatch)
    try:
        campaign_id, account_id = _seed_campaign_with_account(session_factory)
        job_id = we.dm_worker_job_id(campaign_id, account_id)
        redis = FakeRedis(existing_keys={f"arq:in-progress:{job_id}"})

        enqueued = asyncio.run(we._enqueue_dm_workers_with_redis(redis, campaign_id))

        assert redis.enqueued == [], "non deve accodare un secondo job sopra uno gia' in esecuzione"
        assert redis.deleted == [], "non deve cancellare le chiavi di un job in esecuzione"
        assert enqueued == 1, "va comunque contato: un worker per l'account e' gia' attivo"
    finally:
        cleanup()


def test_enqueue_skips_when_retry_is_parked(monkeypatch):
    """Il job di sessione in pausa (Retry(defer=...) parcheggiato) non va cancellato:
    e' esattamente il caso del bug reale (session-break in attesa di ripartire)."""
    session_factory, cleanup = _setup_db(monkeypatch)
    try:
        campaign_id, account_id = _seed_campaign_with_account(session_factory)
        job_id = we.dm_worker_job_id(campaign_id, account_id)
        redis = FakeRedis(existing_keys={f"arq:job:{job_id}", f"arq:retry:{job_id}"})

        enqueued = asyncio.run(we._enqueue_dm_workers_with_redis(redis, campaign_id))

        assert redis.enqueued == []
        assert redis.deleted == []
        assert enqueued == 1
    finally:
        cleanup()


def test_enqueue_proceeds_normally_when_no_job_exists(monkeypatch):
    session_factory, cleanup = _setup_db(monkeypatch)
    try:
        campaign_id, account_id = _seed_campaign_with_account(session_factory)
        redis = FakeRedis()

        enqueued = asyncio.run(we._enqueue_dm_workers_with_redis(redis, campaign_id))

        assert enqueued == 1
        assert len(redis.enqueued) == 1
        (args, kwargs) = redis.enqueued[0]
        assert args[0] == "run_campaign_task"
        assert kwargs["_job_id"] == we.dm_worker_job_id(campaign_id, account_id)
    finally:
        cleanup()
