"""Inbox scraping: i settings di pacing devono esistere con bound cauti."""
from app.config import settings


def test_inbox_api_pacing_present():
    assert settings.inbox_api_page_delay_min_seconds <= settings.inbox_api_page_delay_max_seconds
    assert settings.inbox_api_page_delay_min_seconds >= 1


def test_inbox_api_delay_bounds_updated():
    # Bound 10-40 (commit 156ee47: pacing inbox rallentato, era 2-10) con mediana
    # (min+max)/2 = 25s (lognormale sigma 0.9 in scrape_inbox).
    assert settings.inbox_api_page_delay_min_seconds == 10
    assert settings.inbox_api_page_delay_max_seconds == 40


def test_inbox_session_and_break_bounds():
    assert settings.inbox_session_size >= 10
    assert settings.inbox_break_min_minutes <= settings.inbox_break_max_minutes
