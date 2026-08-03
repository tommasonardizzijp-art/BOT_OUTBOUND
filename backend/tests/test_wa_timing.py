import math
from types import SimpleNamespace

from app.services import wa_timing


def test_wa_send_delay_seconds_stays_in_reasonable_band(monkeypatch):
    """Lognormale centrata su WA_SEND_DELAY_MEDIAN_S: non deve mai andare
    sotto 1s (firma robotica) ne' esplodere oltre 20 minuti (bug di sigma)."""
    from app.config import settings
    monkeypatch.setattr(settings, "wa_send_delay_median_s", 90)
    monkeypatch.setattr(settings, "wa_send_delay_sigma", 0.7)
    samples = [wa_timing.wa_send_delay_seconds() for _ in range(200)]
    assert all(1.0 <= s <= 1200.0 for s in samples)
    # non deve essere una costante (firma robotica identica a ogni chiamata)
    assert len(set(round(s, 1) for s in samples)) > 50


def test_wa_session_message_count_uses_campaign_override_when_set():
    campaign = SimpleNamespace(session_min_messages=3, session_max_messages=3,
                                break_min_minutes=None, break_max_minutes=None)
    for _ in range(20):
        assert wa_timing.wa_session_message_count(campaign) == 3


def test_wa_session_message_count_falls_back_to_settings_when_campaign_null(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "wa_session_min_msg", 8)
    monkeypatch.setattr(settings, "wa_session_max_msg", 8)
    campaign = SimpleNamespace(session_min_messages=None, session_max_messages=None,
                                break_min_minutes=None, break_max_minutes=None)
    assert wa_timing.wa_session_message_count(campaign) == 8


def test_wa_session_break_seconds_campaign_override(monkeypatch):
    campaign = SimpleNamespace(session_min_messages=None, session_max_messages=None,
                                break_min_minutes=1, break_max_minutes=1)
    samples = [wa_timing.wa_session_break_seconds(campaign) for _ in range(20)]
    assert all(abs(s - 60.0) < 5.0 for s in samples)


def test_effective_wa_active_hours_parses_HHMM_range(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "wa_active_hours", "09:30-19:30")
    campaign = SimpleNamespace(active_hours_start=None, active_hours_end=None)
    assert wa_timing.effective_wa_active_hours(campaign) == (9, 19)


def test_effective_wa_active_hours_campaign_override():
    campaign = SimpleNamespace(active_hours_start="08:00", active_hours_end="12:00")
    assert wa_timing.effective_wa_active_hours(campaign) == (8, 12)
