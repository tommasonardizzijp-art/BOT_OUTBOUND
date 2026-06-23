"""Inbox: cambio engine azzera il cursore intra-engine (resume-by-frontier)."""
from app.api.campaigns import engine_switch_resets_cursor


def test_switch_resets_cursor():
    assert engine_switch_resets_cursor("browser", "api") is True
    assert engine_switch_resets_cursor("api", "browser") is True


def test_same_engine_keeps_cursor():
    assert engine_switch_resets_cursor("browser", "browser") is False
    assert engine_switch_resets_cursor("api", "api") is False
