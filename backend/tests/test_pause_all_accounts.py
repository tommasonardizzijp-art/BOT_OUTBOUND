"""L'attivita' browser in pausa deve coprire TUTTI gli account scraping della
campagna (non solo l'ultimo usato), in parallelo ma scaglionati."""
import asyncio

import app.services.browser_bio as bb


def test_all_accounts_get_a_session(monkeypatch):
    called = []

    async def fake_activity(campaign_id, account_id, username=None):
        called.append(account_id)
        return 1

    async def fake_accounts(campaign_id):
        return [("a1", "u1"), ("a2", "u2"), ("a3", "u3")]

    async def no_sleep(_):
        return None

    monkeypatch.setattr(bb.settings, "bio_browser_batch_enabled", True)
    monkeypatch.setattr(bb, "run_pause_browser_activity", fake_activity)
    monkeypatch.setattr(bb, "_scraping_accounts_of_campaign", fake_accounts)
    monkeypatch.setattr(bb.asyncio, "sleep", no_sleep)

    asyncio.run(bb.run_pause_browser_all_accounts("camp1"))
    assert set(called) == {"a1", "a2", "a3"}  # tutti coperti


def test_noop_when_all_flags_off(monkeypatch):
    called = []

    async def fake_activity(campaign_id, account_id, username=None):
        called.append(account_id)
        return 1

    async def fake_accounts(campaign_id):
        return [("a1", "u1")]

    monkeypatch.setattr(bb.settings, "warmup_browse_enabled", False)
    monkeypatch.setattr(bb.settings, "bio_browser_batch_enabled", False)
    monkeypatch.setattr(bb, "run_pause_browser_activity", fake_activity)
    monkeypatch.setattr(bb, "_scraping_accounts_of_campaign", fake_accounts)

    spent = asyncio.run(bb.run_pause_browser_all_accounts("camp1"))
    assert spent == 0
    assert called == []  # niente sessioni se tutto OFF
