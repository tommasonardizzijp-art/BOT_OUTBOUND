"""Fase Lista: dimensione pagina e delay devono leggere dai settings con bound corretti."""
from app.config import settings


def test_cap_default_raised_to_300():
    assert settings.scrape_daily_limit == 300


def test_list_page_settings_present_and_sane():
    assert settings.list_page_size_min == 20
    assert settings.list_page_size_max == 40
    assert settings.list_page_size_min <= settings.list_page_size_max
    assert settings.list_page_delay_min_seconds == 5
    assert settings.list_page_delay_max_seconds == 10
    assert 0.0 <= settings.list_long_pause_probability <= 1.0
    assert settings.list_long_pause_min_seconds <= settings.list_long_pause_max_seconds
