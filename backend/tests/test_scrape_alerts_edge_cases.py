"""Casi limite del ponte alert scraping->Telegram + e2e della catena completa.

Copre cio' che i test unitari (test_scrape_stop_alerts, test_scrape_warning_alerts)
non toccano: emit fuori da un event loop, hook che non deve mai rompere emit,
scadenza del throttle, kind sconosciuto, risoluzione nome campagna su DB vero
(sqlite), e la catena e2e loop bio reale -> emit -> hook -> messaggio formattato.
"""
import asyncio
import os
import tempfile
import uuid

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.database import Base
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
import app.models.account  # noqa: F401
import app.models.campaign_account  # noqa: F401
import app.models.message  # noqa: F401
import app.models.activity_log  # noqa: F401
import app.models.global_contact  # noqa: F401

from app.services import notifier, scrape_bios
from app.utils import events


# ---------------------------------------------------------------------------
# emit(): l'hook non deve MAI rompere il chiamante
# ---------------------------------------------------------------------------

def test_emit_without_event_loop_does_not_raise():
    """emit() da contesto sync puro (nessun loop): l'hook deve fare no-op,
    non esplodere — emit e' dichiarata 'safe to call from any process'."""
    events.emit("camp-x", "scrape_stopped", "Errore finto", level="error")


@pytest.mark.asyncio
async def test_emit_survives_broken_notifier(monkeypatch):
    """Se lo scheduling del task fallisce (es. notifier rotto: create_task su
    non-coroutine -> TypeError), emit deve comunque completare in silenzio."""
    monkeypatch.setattr(notifier, "send_scrape_stop_alert", lambda cid, d: "non-coroutine")
    events.emit("camp-x", "scrape_stopped", "Errore finto", level="error")
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Throttle warning: scadenza cooldown
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_warning_throttle_expires_after_cooldown(monkeypatch):
    calls: list[str] = []

    async def _fake_send_telegram(message, level="info", *, reply_markup=None):
        calls.append(message)

    async def _fake_resolve(campaign_id):
        return None

    monkeypatch.setattr(notifier, "send_telegram", _fake_send_telegram)
    monkeypatch.setattr(notifier, "_resolve_campaign_name", _fake_resolve)
    notifier._warn_last_sent.clear()

    now = {"t": 0.0}
    monkeypatch.setattr(notifier.time, "monotonic", lambda: now["t"])

    now["t"] = 0.0
    await notifier.send_scrape_warning_alert("camp-1", "soft_block")  # t=0: invia
    now["t"] = 100.0
    await notifier.send_scrape_warning_alert("camp-1", "soft_block")  # t=100: muto
    now["t"] = 100.0 + notifier._WARN_COOLDOWN_SECONDS + 1
    await notifier.send_scrape_warning_alert("camp-1", "soft_block")  # cooldown scaduto: invia

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_warning_unknown_kind_uses_default_copy(monkeypatch):
    sent: dict = {}

    async def _fake_send_telegram(message, level="info", *, reply_markup=None):
        sent["message"] = message
        sent["reply_markup"] = reply_markup

    async def _fake_resolve(campaign_id):
        return None

    monkeypatch.setattr(notifier, "send_telegram", _fake_send_telegram)
    monkeypatch.setattr(notifier, "_resolve_campaign_name", _fake_resolve)
    notifier._warn_last_sent.clear()

    await notifier.send_scrape_warning_alert("camp-1", "kind_futuro_mai_visto")

    assert "Scraping: serve un controllo" in sent["message"]
    assert sent["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "pause:camp-1"


# ---------------------------------------------------------------------------
# Risoluzione nome campagna su DB vero (sqlite al posto di Supabase)
# ---------------------------------------------------------------------------

def _make_sqlite_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="alertedge_")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", connect_args={"check_same_thread": False})
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    def cleanup():
        asyncio.run(engine.dispose())
        try:
            os.remove(path)
        except OSError:
            pass

    return engine, session_factory, cleanup


def test_stop_alert_resolves_real_campaign_name_from_db(monkeypatch):
    """_resolve_campaign_name NON mockata: legge davvero dal DB (sqlite)."""
    engine, session_factory, cleanup = _make_sqlite_db()
    campaign_id = str(uuid.uuid4())
    try:
        async def _go():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with session_factory() as db:
                db.add(Campaign(
                    id=campaign_id, name="Campagna Vera", source_type="scrape",
                    target_username="t", status=CampaignStatus.scraping,
                    messaging_enabled=False,
                ))
                await db.commit()

            import app.database as app_database
            monkeypatch.setattr(app_database, "AsyncSessionLocal", session_factory)

            sent: dict = {}

            async def _fake_send_telegram(message, level="info", *, reply_markup=None):
                sent["message"] = message

            monkeypatch.setattr(notifier, "send_telegram", _fake_send_telegram)
            await notifier.send_scrape_stop_alert(campaign_id, "Connessione persa")
            return sent

        sent = asyncio.run(_go())
        assert "Campagna Vera" in sent["message"]
        assert campaign_id not in sent["message"]  # col nome risolto niente ID grezzo
    finally:
        cleanup()


def test_stop_alert_missing_campaign_falls_back_to_id(monkeypatch):
    """Campagna inesistente nel DB: lookup ritorna None -> messaggio con l'ID."""
    engine, session_factory, cleanup = _make_sqlite_db()
    try:
        async def _go():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

            import app.database as app_database
            monkeypatch.setattr(app_database, "AsyncSessionLocal", session_factory)

            sent: dict = {}

            async def _fake_send_telegram(message, level="info", *, reply_markup=None):
                sent["message"] = message

            monkeypatch.setattr(notifier, "send_telegram", _fake_send_telegram)
            await notifier.send_scrape_stop_alert("ghost-campaign", "Errore")
            return sent

        sent = asyncio.run(_go())
        assert "ghost-campaign" in sent["message"]
    finally:
        cleanup()


# ---------------------------------------------------------------------------
# E2E: loop bio REALE -> stop rete -> emit reale -> hook -> messaggio Telegram
# ---------------------------------------------------------------------------

class _FakePool:
    @classmethod
    async def build(cls, db, campaign):
        return cls()

    def next(self, campaign):
        return ("fake_account", "fake_client")

    async def save_sessions(self, db):
        pass

    async def release(self):
        pass


def test_e2e_network_stop_produces_formatted_telegram_alert(monkeypatch):
    """Catena completa senza mock intermedi: scrape_bios reale con rete giu'
    -> emit() reale -> hook events -> send_scrape_stop_alert reale (nome da
    sqlite) -> send_telegram (unico punto finto, cattura il messaggio)."""
    engine, session_factory, cleanup = _make_sqlite_db()
    campaign_id = str(uuid.uuid4())
    try:
        async def _go():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with session_factory() as db:
                db.add(Campaign(
                    id=campaign_id, name="E2E Rete Giu", source_type="scrape",
                    target_username="t", status=CampaignStatus.scraping,
                    messaging_enabled=False,
                ))
                db.add(Follower(
                    id=str(uuid.uuid4()), campaign_id=campaign_id,
                    ig_user_id=920000001, username="lead_e2e",
                    status=FollowerStatus.pending,
                ))
                await db.commit()

            import app.database as app_database
            monkeypatch.setattr(app_database, "AsyncSessionLocal", session_factory)
            monkeypatch.setattr(scrape_bios, "AsyncSessionLocal", session_factory)
            monkeypatch.setattr(scrape_bios, "ScrapingPool", _FakePool)

            async def _not_halted(db):
                return False
            monkeypatch.setattr(scrape_bios, "is_halted", _not_halted)

            async def _fetch(follower, campaign, db, pool):
                return "network", None, ConnectionError("ProxyError e2e")
            monkeypatch.setattr(scrape_bios, "fetch_and_store_bio", _fetch)

            # Warning flap: fuori scope qui, muto.
            async def _no_warn(cid, kind, detail=""):
                pass
            monkeypatch.setattr(scrape_bios, "send_scrape_warning_alert", _no_warn)

            sent: dict = {}

            async def _fake_send_telegram(message, level="info", *, reply_markup=None):
                sent["message"] = message
                sent["level"] = level
            monkeypatch.setattr(notifier, "send_telegram", _fake_send_telegram)

            _real_sleep = asyncio.sleep

            async def _fast_sleep(_):
                await _real_sleep(0)
            monkeypatch.setattr(scrape_bios.asyncio, "sleep", _fast_sleep)
            monkeypatch.setattr(scrape_bios.random, "uniform", lambda a, b: 0)

            await asyncio.wait_for(scrape_bios.scrape_bios(campaign_id), timeout=10)
            # Flush del task fire-and-forget schedulato dall'hook di emit().
            for _ in range(20):
                if sent:
                    break
                await _real_sleep(0.01)
            return sent

        sent = asyncio.run(_go())
        assert sent, "nessun messaggio Telegram: la catena emit->hook->alert non e' scattata"
        assert sent["level"] == "error"
        assert "Scraping fermato" in sent["message"]
        assert "E2E Rete Giu" in sent["message"]
        assert "Connessione persa" in sent["message"]
    finally:
        cleanup()
