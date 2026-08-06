"""Riavvio campagna durante la pausa di sessione: il lease parcheggiato non deve bloccare.

Bug reale (06/08, campagna FLASH LASER): a fine batch `_defer_next_batch` prolunga il
lease dell'account per tutta la pausa (30 min) e NON lo rilascia all'uscita, poi
parcheggia il job con `Retry(defer)`. Se l'operatore riavvia/riprende la campagna
prima della scadenza, l'enqueue cancella la retry parcheggiata e accoda un job nuovo
con un `lease_owner` nuovo -> `acquire` trova il lease del job morto e il worker esce
subito con "already leased by another job". Nessun worker resta in coda: la campagna
sembra viva ma non manda piu' niente finche' non si spegne tutto il bot.

Regola: il lease e' di uno SLOT di lavoro (`worker:{campaign}:{account}`), non della
singola invocazione. Se ARQ non ha quel job in esecuzione, lo slot e' libero e il
lease va rilasciato prima di accodare il worker nuovo.
"""
import asyncio
import os
import tempfile
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.account import InstagramAccount, AccountStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.campaign_account import CampaignAccount


CAMPAIGN_ID = "camp-lease-1"


class _FakeRedis:
    def __init__(self, in_progress: set[str] | None = None):
        self.enqueued = []
        self.deleted = []
        self._in_progress = in_progress or set()

    async def exists(self, *keys):
        return sum(1 for k in keys if k in self._in_progress)

    async def delete(self, *keys):
        self.deleted.extend(keys)

    async def enqueue_job(self, fn, *args, **kwargs):
        self.enqueued.append((fn, args, kwargs))


async def _setup_db(monkeypatch):
    from app.database import Base
    import app.services.work_enqueue as we

    fd, path = tempfile.mkstemp(suffix=".db", prefix="lease_handoff_")
    os.close(fd)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{path}", connect_args={"check_same_thread": False}
    )
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    monkeypatch.setattr(we, "AsyncSessionLocal", session_factory)

    async def cleanup():
        await engine.dispose()
        try:
            os.remove(path)
        except OSError:
            pass

    return session_factory, cleanup


async def _add_dm_account(session_factory, *, lease_owner, lease_expires_at, campaign_id=CAMPAIGN_ID):
    acc_id = str(uuid.uuid4())

    async with session_factory() as db:
        if not await db.get(Campaign, campaign_id):
            db.add(Campaign(
                id=campaign_id, name="lease handoff",
                source_type="scrape", status=CampaignStatus.running,
            ))
        db.add(InstagramAccount(
            id=acc_id, username=f"acc_{acc_id[:6]}",
            encrypted_password="x", status=AccountStatus.active,
            daily_message_limit=20,
            lease_owner=lease_owner, lease_expires_at=lease_expires_at,
        ))
        db.add(CampaignAccount(
            id=str(uuid.uuid4()), campaign_id=campaign_id,
            account_id=acc_id, role="dm", is_active=True,
        ))
        await db.commit()
    return acc_id


async def _lease_of(session_factory, acc_id):
    async with session_factory() as db:
        a = await db.get(InstagramAccount, acc_id)
        return a.lease_owner, a.lease_expires_at


@pytest.mark.asyncio
async def test_enqueue_rilascia_il_lease_dello_slot_parcheggiato(monkeypatch):
    """Job NON in esecuzione (parcheggiato in defer): il lease dello stesso slot
    va rilasciato, altrimenti il worker nuovo esce con 'already leased'."""
    import app.services.work_enqueue as we
    session_factory, cleanup = await _setup_db(monkeypatch)
    try:
        acc_id = await _add_dm_account(
            session_factory,
            lease_owner=None, lease_expires_at=None,
        )
        # lease del job parcheggiato: stesso slot, owner di invocazione diverso,
        # scadenza ancora nel futuro (dura quanto la pausa di sessione).
        async def _park():
            async with session_factory() as db:
                acc = await db.get(InstagramAccount, acc_id)
                acc.lease_owner = f"worker:{CAMPAIGN_ID}:{acc_id}:deadbeef"
                acc.lease_expires_at = datetime.utcnow() + timedelta(minutes=25)
                await db.commit()
        await _park()

        r = _FakeRedis()
        enqueued = await we._enqueue_dm_workers_with_redis(r, CAMPAIGN_ID)

        assert enqueued == 1
        assert len(r.enqueued) == 1
        owner, exp = await _lease_of(session_factory, acc_id)
        assert owner is None, f"lease dello slot non rilasciato: {owner!r}"
        assert exp is None
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_enqueue_non_tocca_il_lease_di_un_worker_in_esecuzione(monkeypatch):
    """Job in esecuzione: il lease resta al worker vivo e il suo lock ARQ non si tocca.

    L'enqueue avviene comunque (ARQ ripesca il job appena il lock sparisce, senza
    eseguirne due): saltarlo perderebbe il worker se il job stesse uscendo proprio
    in quell'istante. Quello che NON si puo' fare e' cancellare l'in-progress o
    rubare il lease — sarebbero due sessioni browser sullo stesso profilo."""
    import app.services.work_enqueue as we
    session_factory, cleanup = await _setup_db(monkeypatch)
    try:
        acc_id = await _add_dm_account(session_factory, lease_owner=None, lease_expires_at=None)
        live_owner = f"worker:{CAMPAIGN_ID}:{acc_id}:cafe0000"
        expires = datetime.utcnow() + timedelta(minutes=14)

        async def _live():
            async with session_factory() as db:
                acc = await db.get(InstagramAccount, acc_id)
                acc.lease_owner = live_owner
                acc.lease_expires_at = expires
                await db.commit()
        await _live()

        job_id = f"worker:{CAMPAIGN_ID}:{acc_id}"
        r = _FakeRedis(in_progress={f"arq:in-progress:{job_id}"})
        enqueued = await we._enqueue_dm_workers_with_redis(r, CAMPAIGN_ID)

        assert enqueued == 1
        assert f"arq:in-progress:{job_id}" not in r.deleted, "cancellato il lock ARQ del worker vivo"
        owner, _ = await _lease_of(session_factory, acc_id)
        assert owner == live_owner, "rubato il lease a un worker vivo"
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_enqueue_non_tocca_lease_di_altra_campagna(monkeypatch):
    """Account in uso da un'ALTRA campagna: il lease non si tocca — il worker nuovo
    deve uscire con 'already leased' (un account alla volta)."""
    import app.services.work_enqueue as we
    session_factory, cleanup = await _setup_db(monkeypatch)
    try:
        acc_id = await _add_dm_account(session_factory, lease_owner=None, lease_expires_at=None)
        other_owner = f"worker:altra-campagna:{acc_id}:99999999"

        async def _other():
            async with session_factory() as db:
                acc = await db.get(InstagramAccount, acc_id)
                acc.lease_owner = other_owner
                acc.lease_expires_at = datetime.utcnow() + timedelta(minutes=20)
                await db.commit()
        await _other()

        r = _FakeRedis()
        await we._enqueue_dm_workers_with_redis(r, CAMPAIGN_ID)

        owner, _ = await _lease_of(session_factory, acc_id)
        assert owner == other_owner, "rubato il lease a un'altra campagna"
    finally:
        await cleanup()
