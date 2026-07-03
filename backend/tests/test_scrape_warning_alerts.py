"""Warning Telegram per condizioni scraping che richiedono un occhio umano
MENTRE la run continua: 429/soft-block e proxy/rete che flappa.

Distinti dagli alert di stop (test_scrape_stop_alerts): qui il bot NON si e'
fermato, ma insistere puo' stuzzicare Instagram a vuoto. Il warning offre il
bottone inline "pausa campagna" (callback pause:{id} gia' gestita) e /halt.
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


# ---------------------------------------------------------------------------
# send_scrape_warning_alert (notifier)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_warning_has_pause_button_and_halt_hint(monkeypatch):
    sent: dict = {}

    async def _fake_send_telegram(message: str, level: str = "info", *, reply_markup=None):
        sent["message"] = message
        sent["level"] = level
        sent["reply_markup"] = reply_markup

    async def _fake_resolve(campaign_id: str):
        return "PRIMERO Outreach"

    monkeypatch.setattr(notifier, "send_telegram", _fake_send_telegram)
    monkeypatch.setattr(notifier, "_resolve_campaign_name", _fake_resolve)
    notifier._warn_last_sent.clear()

    await notifier.send_scrape_warning_alert("camp-1", "soft_block", "@user via @acc: 429")

    assert sent["level"] == "warning"
    assert "PRIMERO Outreach" in sent["message"]
    assert "429" in sent["message"]
    assert "/halt" in sent["message"]
    buttons = sent["reply_markup"]["inline_keyboard"][0]
    assert any(b["callback_data"] == "pause:camp-1" for b in buttons)


@pytest.mark.asyncio
async def test_warning_is_throttled_per_campaign_and_kind(monkeypatch):
    calls: list[str] = []

    async def _fake_send_telegram(message: str, level: str = "info", *, reply_markup=None):
        calls.append(message)

    async def _fake_resolve(campaign_id: str):
        return None

    monkeypatch.setattr(notifier, "send_telegram", _fake_send_telegram)
    monkeypatch.setattr(notifier, "_resolve_campaign_name", _fake_resolve)
    notifier._warn_last_sent.clear()

    await notifier.send_scrape_warning_alert("camp-1", "soft_block")
    await notifier.send_scrape_warning_alert("camp-1", "soft_block")   # muto: cooldown
    await notifier.send_scrape_warning_alert("camp-1", "network_flaky")  # kind diverso: passa
    await notifier.send_scrape_warning_alert("camp-2", "soft_block")   # campagna diversa: passa

    assert len(calls) == 3


# ---------------------------------------------------------------------------
# Trigger dentro il loop scrape_bios
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


def _setup(monkeypatch, outcomes: list[str]):
    """Pilota il loop reale di scrape_bios con outcome scriptati per chiamata.

    Esauriti gli outcome, ritorna 'done'. Ritorna (campaign_id, warnings, cleanup):
    warnings raccoglie le chiamate a send_scrape_warning_alert.
    """
    fd, path = tempfile.mkstemp(suffix=".db", prefix="biowarn_")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", connect_args={"check_same_thread": False})
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    campaign_id = str(uuid.uuid4())

    async def _seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_factory() as db:
            db.add(Campaign(
                id=campaign_id,
                name="warn test",
                source_type="scrape",
                target_username="target",
                status=CampaignStatus.scraping,
                messaging_enabled=False,
            ))
            for i in range(4):
                db.add(Follower(
                    id=str(uuid.uuid4()),
                    campaign_id=campaign_id,
                    ig_user_id=910000000 + i,
                    username=f"user_{i}",
                    status=FollowerStatus.pending,
                ))
            await db.commit()

    asyncio.run(_seed())

    monkeypatch.setattr(scrape_bios, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(scrape_bios, "ScrapingPool", _FakePool)

    async def _not_halted(db):
        return False
    monkeypatch.setattr(scrape_bios, "is_halted", _not_halted)

    script = list(outcomes)

    class _FakeAccount:
        username = "fake_account"

    async def _fetch(follower, campaign, db, pool):
        outcome = script.pop(0) if script else "done"
        if outcome == "done":
            follower.status = FollowerStatus.bio_scraped
            await db.commit()
            return "done", _FakeAccount(), None
        return outcome, _FakeAccount(), RuntimeError("ProxyError finto")
    monkeypatch.setattr(scrape_bios, "fetch_and_store_bio", _fetch)

    warnings: list[tuple[str, str]] = []

    async def _fake_warning(cid, kind, detail=""):
        warnings.append((cid, kind))
    monkeypatch.setattr(scrape_bios, "send_scrape_warning_alert", _fake_warning)

    monkeypatch.setattr("app.utils.events.emit", lambda *a, **k: None)

    _real_sleep = asyncio.sleep

    async def _fast_sleep(_):
        await _real_sleep(0)
    monkeypatch.setattr(scrape_bios.asyncio, "sleep", _fast_sleep)
    monkeypatch.setattr(scrape_bios.random, "uniform", lambda a, b: 0)

    def cleanup():
        asyncio.run(engine.dispose())
        try:
            os.remove(path)
        except OSError:
            pass

    return campaign_id, warnings, cleanup


def test_soft_block_triggers_warning_each_time(monkeypatch):
    """Ogni 429/soft-block avvisa (il throttle sta nel notifier, non qui)."""
    campaign_id, warnings, cleanup = _setup(monkeypatch, ["soft_block", "done", "soft_block"])
    try:
        asyncio.run(asyncio.wait_for(scrape_bios.scrape_bios(campaign_id), timeout=10))
        assert warnings.count((campaign_id, "soft_block")) == 2
    finally:
        cleanup()


def test_network_flap_warns_at_threshold_even_if_recovering(monkeypatch):
    """Errori di rete che i retry recuperano: al 3o nella run parte il warning
    'proxy instabile' anche se nessun profilo viene perso e la run continua."""
    # 2 flap su user_0 (poi done), 2 flap su user_1 (poi done): 4 errori totali,
    # mai 3 sullo stesso profilo -> nessuno stop, ma soglia run-level superata.
    campaign_id, warnings, cleanup = _setup(
        monkeypatch,
        ["network", "network", "done", "network", "network", "done"],
    )
    try:
        asyncio.run(asyncio.wait_for(scrape_bios.scrape_bios(campaign_id), timeout=10))
        assert (campaign_id, "network_flaky") in warnings
    finally:
        cleanup()


def test_few_network_blips_do_not_warn(monkeypatch):
    """1-2 blip di rete assorbiti dai retry: nessun warning (rumore)."""
    campaign_id, warnings, cleanup = _setup(monkeypatch, ["network", "done", "network", "done"])
    try:
        asyncio.run(asyncio.wait_for(scrape_bios.scrape_bios(campaign_id), timeout=10))
        assert (campaign_id, "network_flaky") not in warnings
    finally:
        cleanup()
