"""Fase Lista: page-size FISSO (= tetto reale endpoint, misurato ~25) e delay coi bound giusti."""
from app.config import settings


def test_cap_default_raised_to_300():
    assert settings.scrape_daily_limit == 300


def test_list_page_settings_present_and_sane():
    # count FISSO (non piu' min/max random): misurato ~25 utenti/risposta.
    assert settings.list_page_size == 25
    assert settings.list_page_delay_min_seconds == 5
    assert settings.list_page_delay_max_seconds == 10
    assert 0.0 <= settings.list_long_pause_probability <= 1.0
    assert settings.list_long_pause_min_seconds <= settings.list_long_pause_max_seconds


def test_next_page_size_is_fixed():
    from app.services.scrape_list import next_page_size
    # Deve ritornare SEMPRE lo stesso valore (mai random): un count variabile e' una firma.
    vals = {next_page_size() for _ in range(100)}
    assert vals == {settings.list_page_size}


def test_list_remaining_respects_target():
    from app.services.scrape_list import remaining_for_target
    # target None = illimitato -> ritorna il page size proposto
    assert remaining_for_target(target=None, already=100, page=25) == 25
    # target 500, gia' 490 -> al massimo 10
    assert remaining_for_target(target=500, already=490, page=25) == 10
    # target raggiunto -> 0
    assert remaining_for_target(target=500, already=500, page=25) == 0
