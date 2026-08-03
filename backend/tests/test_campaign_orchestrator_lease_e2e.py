"""E2E + adversarial coverage for the lease-contention fix in run_campaign_worker.

Bug reale: quando account_lease.acquire() falliva, il worker faceva `return`
secco — nessun retry schedulato, nessuno lo richiamava mai piu'. Ora deve
`raise Retry(defer=LEASE_CONTENTION_RETRY_SECONDS)`. Questi test guidano
run_campaign_worker per intero (DB sqlite reale, ARQ Retry reale) attraverso
i due punti dove il lease viene acquisito, sia nel caso "bloccato" che nel
caso "libero", per essere sicuri che il guardrail nuovo non rompa il percorso
normale e non si limiti a coprire il caso felice.
"""
import asyncio
import os
import tempfile
import uuid
from datetime import datetime, timedelta

import pytest
from arq.worker import Retry
from arq.utils import to_ms
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.account import InstagramAccount, AccountStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.campaign_account import CampaignAccount
import app.services.campaign_orchestrator as co
from app.services.campaign_orchestrator import (
    run_campaign_worker,
    LEASE_CONTENTION_RETRY_SECONDS,
)
from app.services import account_lease


def _setup_db(monkeypatch):
    from app.database import Base

    fd, path = tempfile.mkstemp(suffix=".db", prefix="lease_e2e_")
    os.close(fd)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{path}", connect_args={"check_same_thread": False}
    )
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_seed())
    monkeypatch.setattr(co, "AsyncSessionLocal", session_factory)
    # Anche il pre-stagger check (_pre_db) e il rilascio lease nel finally usano
    # AsyncSessionLocal importato altrove: patchamo anche i moduli che lo importano.
    import app.database as database_module
    monkeypatch.setattr(database_module, "AsyncSessionLocal", session_factory)

    def cleanup():
        asyncio.run(engine.dispose())
        try:
            os.remove(path)
        except OSError:
            pass

    return session_factory, cleanup


def _seed_campaign_and_account(session_factory, *, status=CampaignStatus.running):
    campaign_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())

    async def _go():
        async with session_factory() as db:
            db.add(Campaign(
                id=campaign_id, name="Test", target_username="target",
                base_message_template="Messaggio abbastanza lungo",
                messaging_enabled=True,
                status=status,
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


def _set_lease(session_factory, account_id, *, owner, expires_at):
    async def _go():
        async with session_factory() as db:
            acc = await db.get(InstagramAccount, account_id)
            acc.lease_owner = owner
            acc.lease_expires_at = expires_at
            await db.commit()

    asyncio.run(_go())


def _get_account(session_factory, account_id):
    async def _go():
        async with session_factory() as db:
            return await db.get(InstagramAccount, account_id)

    return asyncio.run(_go())


def _get_campaign(session_factory, campaign_id):
    async def _go():
        async with session_factory() as db:
            return await db.get(Campaign, campaign_id)

    return asyncio.run(_go())


def _quiet(monkeypatch):
    """Niente Redis/Telegram reali durante i test: emit_event no-op."""
    monkeypatch.setattr(co, "emit_event", lambda *a, **k: None)
    monkeypatch.setattr(co._random if hasattr(co, "_random") else __import__("random"), "uniform", lambda a, b: 0)
    monkeypatch.setattr("random.uniform", lambda a, b: 0)  # startup jitter: 0s, non 0-10s reali


# ─────────────────────── 1. Lease bloccata da uno sconosciuto ───────────────────────

def test_e2e_lease_held_by_stranger_retries_instead_of_dying(monkeypatch):
    """Caso del bug reale: lease valida di un altro job → deve riprovare tra
    LEASE_CONTENTION_RETRY_SECONDS, non sparire nel nulla."""
    _quiet(monkeypatch)
    session_factory, cleanup = _setup_db(monkeypatch)
    try:
        campaign_id, account_id = _seed_campaign_and_account(session_factory)
        stranger_expiry = datetime.utcnow() + timedelta(minutes=10)
        _set_lease(session_factory, account_id, owner="worker:other:xyz", expires_at=stranger_expiry)

        with pytest.raises(Retry) as exc:
            asyncio.run(run_campaign_worker(campaign_id, account_id))

        assert exc.value.defer_score == to_ms(LEASE_CONTENTION_RETRY_SECONDS)

        # Adversarial: la lease dello sconosciuto non deve essere toccata.
        acc = _get_account(session_factory, account_id)
        assert acc.lease_owner == "worker:other:xyz"
        assert acc.lease_expires_at == stranger_expiry

        # La campagna resta running: deve restare "riprovabile", non bloccarsi
        # in uno stato che l'operatore deve sbloccare a mano.
        camp = _get_campaign(session_factory, campaign_id)
        assert camp.status == CampaignStatus.running
    finally:
        cleanup()


def test_e2e_lease_held_by_stranger_not_yet_expired_by_one_second(monkeypatch):
    """Adversarial: boundary — lease scade tra 1s, ancora valida ORA. Deve
    comunque riprovare (non deve né bloccarsi per sempre né rubarla in anticipo)."""
    _quiet(monkeypatch)
    session_factory, cleanup = _setup_db(monkeypatch)
    try:
        campaign_id, account_id = _seed_campaign_and_account(session_factory)
        _set_lease(
            session_factory, account_id,
            owner="worker:other:xyz",
            expires_at=datetime.utcnow() + timedelta(seconds=1),
        )

        with pytest.raises(Retry) as exc:
            asyncio.run(run_campaign_worker(campaign_id, account_id))

        assert exc.value.defer_score == to_ms(LEASE_CONTENTION_RETRY_SECONDS)
    finally:
        cleanup()


# ─────────────────────── 2. Lease libera: il fix non deve bloccare il percorso normale ───────────────────────

def test_e2e_lease_expired_is_reclaimed_and_worker_proceeds(monkeypatch):
    """Adversarial contro il fix stesso: una lease scaduta (owner morto) NON deve
    finire nel ramo di retry — va riacquisita e il worker deve proseguire
    normalmente (qui: fino alla pausa di sessione, ramo pre-esistente)."""
    _quiet(monkeypatch)
    from app.services import human_behavior
    monkeypatch.setattr(human_behavior.SessionManager, "is_active_hour", lambda self: True)
    monkeypatch.setattr(human_behavior.SessionManager, "should_break_session", lambda self: True)

    session_factory, cleanup = _setup_db(monkeypatch)
    try:
        campaign_id, account_id = _seed_campaign_and_account(session_factory)
        _set_lease(
            session_factory, account_id,
            owner="worker:dead:xyz",
            expires_at=datetime.utcnow() - timedelta(minutes=1),  # scaduta
        )

        with pytest.raises(Retry) as exc:
            asyncio.run(run_campaign_worker(campaign_id, account_id))

        # Non e' il nostro retry da contesa (120s): e' _defer_next_batch (session break).
        assert exc.value.defer_score != to_ms(LEASE_CONTENTION_RETRY_SECONDS)

        # La lease scaduta e' stata riacquisita DA NOI (job_id come prefisso owner)
        # e tenuta per la pausa — prova che il worker e' passato oltre il gate.
        acc = _get_account(session_factory, account_id)
        assert acc.lease_owner is not None
        assert acc.lease_owner.startswith(f"worker:{campaign_id}:{account_id}:")
        assert acc.lease_expires_at > datetime.utcnow()
    finally:
        cleanup()


def test_e2e_lease_free_null_is_acquired_and_worker_proceeds(monkeypatch):
    """Stesso scenario ma con lease mai presa (owner NULL) invece che scaduta —
    ramo diverso della or_() in account_lease.acquire, va coperto separatamente."""
    _quiet(monkeypatch)
    from app.services import human_behavior
    monkeypatch.setattr(human_behavior.SessionManager, "is_active_hour", lambda self: True)
    monkeypatch.setattr(human_behavior.SessionManager, "should_break_session", lambda self: True)

    session_factory, cleanup = _setup_db(monkeypatch)
    try:
        campaign_id, account_id = _seed_campaign_and_account(session_factory)
        # Nessuna _set_lease: lease_owner/lease_expires_at restano NULL di default.

        with pytest.raises(Retry) as exc:
            asyncio.run(run_campaign_worker(campaign_id, account_id))

        assert exc.value.defer_score != to_ms(LEASE_CONTENTION_RETRY_SECONDS)
        acc = _get_account(session_factory, account_id)
        assert acc.lease_owner is not None
    finally:
        cleanup()


# ─────────────────────── 3. Secondo gate: dopo l'attesa orario attivo ───────────────────────

def test_e2e_lease_stolen_during_active_hours_wait_retries_not_dies(monkeypatch):
    """Copre il SECONDO punto toccato dal fix (righe post-attesa-orario): il primo
    acquire (prima del check attivo/orario) riesce perche' la lease e' libera;
    fuori orario attivo il worker aspetta; MENTRE aspetta, un altro job ruba la
    lease (TTL 15min scade durante attese lunghe, come dice il commento nel
    codice sorgente). Al ritorno dall'attesa la RI-acquisizione deve fallire e
    riprovare — non sparire in un return silenzioso."""
    _quiet(monkeypatch)
    from app.services import human_behavior
    from sqlalchemy import update as sa_update

    session_factory, cleanup = _setup_db(monkeypatch)
    try:
        campaign_id, account_id = _seed_campaign_and_account(session_factory)

        monkeypatch.setattr(human_behavior.SessionManager, "is_active_hour", lambda self: False)

        async def _fake_wait(self, campaign_id=None, db=None):
            # Furto della lease "durante" l'attesa oraria, sulla stessa sessione
            # db passata dal worker cosi' la mutazione e' visibile subito dopo.
            await db.execute(
                sa_update(InstagramAccount)
                .where(InstagramAccount.id == account_id)
                .values(
                    lease_owner="worker:other:xyz",
                    lease_expires_at=datetime.utcnow() + timedelta(minutes=10),
                )
            )
            await db.commit()
            return True  # campagna ancora attiva dopo l'attesa

        monkeypatch.setattr(human_behavior.SessionManager, "wait_until_active_hours", _fake_wait)

        with pytest.raises(Retry) as exc:
            asyncio.run(run_campaign_worker(campaign_id, account_id))

        assert exc.value.defer_score == to_ms(LEASE_CONTENTION_RETRY_SECONDS)
        acc = _get_account(session_factory, account_id)
        assert acc.lease_owner == "worker:other:xyz", "non deve rubare/alterare la lease altrui"
    finally:
        cleanup()


# ─────────────────────── 4. Adversarial: stato campagna non deve mai innescare retry-loop ───────────────────────

def test_e2e_paused_campaign_never_reaches_lease_code(monkeypatch):
    """Adversarial: una campagna in pausa deve uscire PRIMA del gate lease (return
    pulito, nessun Retry). Se il fix avesse spostato/allargato il gate per errore,
    una campagna in pausa comincerebbe a ri-schedulare se stessa ogni 120s."""
    _quiet(monkeypatch)
    session_factory, cleanup = _setup_db(monkeypatch)
    try:
        campaign_id, account_id = _seed_campaign_and_account(
            session_factory, status=CampaignStatus.paused
        )
        _set_lease(
            session_factory, account_id,
            owner="worker:other:xyz",
            expires_at=datetime.utcnow() + timedelta(minutes=10),
        )

        # Nessuna eccezione: deve tornare pulito, campagna paused ignorata a monte.
        asyncio.run(run_campaign_worker(campaign_id, account_id))

        acc = _get_account(session_factory, account_id)
        assert acc.lease_owner == "worker:other:xyz", "non deve nemmeno provare ad acquisire"
    finally:
        cleanup()


def test_e2e_missing_campaign_never_reaches_lease_code(monkeypatch):
    """Adversarial: campaign_id inesistente (es. cancellata a mano nel frattempo)
    deve uscire pulito, non esplodere ne' entrare nel ramo di retry."""
    _quiet(monkeypatch)
    session_factory, cleanup = _setup_db(monkeypatch)
    try:
        _, account_id = _seed_campaign_and_account(session_factory)
        asyncio.run(run_campaign_worker(str(uuid.uuid4()), account_id))
    finally:
        cleanup()
