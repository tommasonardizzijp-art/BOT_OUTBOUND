"""Inbox scraping: i settings di pacing devono esistere con bound cauti."""
from app.config import settings


def test_inbox_api_pacing_present():
    assert settings.inbox_api_page_delay_min_seconds <= settings.inbox_api_page_delay_max_seconds
    assert settings.inbox_api_page_delay_min_seconds >= 1


def test_inbox_browser_pacing_present():
    assert 2 <= settings.inbox_browser_scroll_min_seconds <= settings.inbox_browser_scroll_max_seconds
    assert settings.inbox_browser_micropause_every_min <= settings.inbox_browser_micropause_every_max
    assert settings.inbox_browser_micropause_min_seconds <= settings.inbox_browser_micropause_max_seconds
    assert 0.0 <= settings.inbox_browser_feedbrowse_probability <= 1.0
    assert settings.inbox_browser_feedbrowse_min_seconds <= settings.inbox_browser_feedbrowse_max_seconds


def test_inbox_session_and_break_bounds():
    assert settings.inbox_session_size >= 10
    assert settings.inbox_break_min_minutes <= settings.inbox_break_max_minutes
