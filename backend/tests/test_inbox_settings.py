"""Inbox scraping: i settings di pacing devono esistere con bound cauti."""
from app.config import settings


def test_inbox_api_pacing_present():
    assert settings.inbox_api_page_delay_min_seconds <= settings.inbox_api_page_delay_max_seconds
    assert settings.inbox_api_page_delay_min_seconds >= 1


def test_inbox_api_delay_bounds_updated():
    # Bound 10-60 (era 10-40, prima 2-10) con lognormale TRONCATA in scrape_inbox:
    # mediana = sqrt(min*max) = 24.5s, sigma 0.9.
    assert settings.inbox_api_page_delay_min_seconds == 10
    assert settings.inbox_api_page_delay_max_seconds == 60


def test_pausa_lunga_sopra_il_range_base():
    """La pausa lunga deve essere una modalita' DISTINTA, non un valore che il
    delay base produce gia' da solo (a 20-60 con base 10-60 era indistinguibile).
    """
    assert settings.inbox_long_pause_min_seconds > settings.inbox_api_page_delay_max_seconds
    assert settings.inbox_long_pause_min_seconds <= settings.inbox_long_pause_max_seconds
    assert 0.0 <= settings.inbox_long_pause_probability <= 1.0


def test_inbox_session_and_break_bounds():
    assert settings.inbox_session_size >= 10
    assert settings.inbox_break_min_minutes <= settings.inbox_break_max_minutes
