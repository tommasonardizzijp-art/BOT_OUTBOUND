"""Config del motore bio browser: default sicuri per il test."""
from app.config import settings


def test_bio_browser_defaults():
    assert settings.bio_browser_headless is False           # test: visibile
    assert 0.0 <= settings.bio_browser_scroll_ratio <= 1.0
    assert settings.bio_browser_scroll_min_s <= settings.bio_browser_scroll_max_s
    assert settings.bio_browser_daily_limit is None          # nessun cap di default
    assert settings.bio_browser_stagger_min_s <= settings.bio_browser_stagger_max_s


def test_bio_browser_session_cap_fits_job_timeout():
    # cap * ~15s/profilo deve stare ben sotto job_timeout=3600s
    assert settings.bio_browser_session_cap_min <= settings.bio_browser_session_cap_max
    assert settings.bio_browser_session_cap_max * 15 < 3600
