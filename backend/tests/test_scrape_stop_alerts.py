"""Gli stop errore della fase scraping (lista/bio/import/challenge) devono
arrivare su Telegram, non solo nel live log UI.

Il ponte sta in events.emit(): ogni scrape_stopped level=error schedula
send_scrape_stop_alert (fire-and-forget). Copre tutti i call site presenti
e futuri senza notifiche sparse nei servizi.
"""
import asyncio

import pytest

from app.services import notifier
from app.utils import events


@pytest.mark.asyncio
async def test_emit_scrape_stopped_error_schedules_telegram_alert(monkeypatch):
    calls: list[tuple[str, str]] = []

    async def _fake_alert(campaign_id: str, detail: str) -> None:
        calls.append((campaign_id, detail))

    monkeypatch.setattr(notifier, "send_scrape_stop_alert", _fake_alert)
    # Redis assente nei test: emit deve comunque arrivare all'hook Telegram.
    events.emit("camp-1", "scrape_stopped", "Connessione persa (tethering/proxy?)", level="error")
    await asyncio.sleep(0)  # flush del task fire-and-forget

    assert calls == [("camp-1", "Connessione persa (tethering/proxy?)")]


@pytest.mark.asyncio
async def test_emit_non_error_or_other_action_does_not_alert(monkeypatch):
    calls: list = []

    async def _fake_alert(campaign_id: str, detail: str) -> None:
        calls.append(campaign_id)

    monkeypatch.setattr(notifier, "send_scrape_stop_alert", _fake_alert)

    events.emit("camp-1", "scrape_stopped", "Bot in pausa globale", level="warn")
    events.emit("camp-1", "scrape_progress", "@x saltato", level="warn")
    events.emit("camp-1", "scrape_complete", "Fase Bio completata")
    await asyncio.sleep(0)

    assert calls == []


@pytest.mark.asyncio
async def test_send_scrape_stop_alert_message_is_operator_readable(monkeypatch):
    sent: dict = {}

    async def _fake_send_telegram(message: str, level: str = "info", **kwargs):
        sent["message"] = message
        sent["level"] = level

    async def _fake_resolve(campaign_id: str) -> str | None:
        return "PRIMERO Outreach"

    monkeypatch.setattr(notifier, "send_telegram", _fake_send_telegram)
    monkeypatch.setattr(notifier, "_resolve_campaign_name", _fake_resolve)

    await notifier.send_scrape_stop_alert("camp-1", "5 bio fallite di fila — interrotta")

    assert sent["level"] == "error"
    assert "Scraping fermato" in sent["message"]
    assert "PRIMERO Outreach" in sent["message"]
    assert "5 bio fallite di fila" in sent["message"]


@pytest.mark.asyncio
async def test_send_scrape_stop_alert_survives_name_lookup_failure(monkeypatch):
    sent: dict = {}

    async def _fake_send_telegram(message: str, level: str = "info", **kwargs):
        sent["message"] = message

    async def _boom(campaign_id: str) -> str | None:
        raise RuntimeError("db down")

    monkeypatch.setattr(notifier, "send_telegram", _fake_send_telegram)
    monkeypatch.setattr(notifier, "_resolve_campaign_name", _boom)

    await notifier.send_scrape_stop_alert("camp-1", "Connessione persa")

    # Fallback: notifica comunque, con l'ID al posto del nome.
    assert "camp-1" in sent["message"]
    assert "Connessione persa" in sent["message"]
