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


def test_next_page_size_within_bounds():
    from app.services.scrape_list import next_page_size
    for _ in range(200):
        n = next_page_size()
        assert 20 <= n <= 40


def test_list_remaining_respects_target():
    from app.services.scrape_list import remaining_for_target
    # target None = illimitato -> ritorna il page size proposto
    assert remaining_for_target(target=None, already=100, page=30) == 30
    # target 500, gia' 480 -> al massimo 20
    assert remaining_for_target(target=500, already=480, page=30) == 20
    # target raggiunto -> 0
    assert remaining_for_target(target=500, already=500, page=30) == 0
