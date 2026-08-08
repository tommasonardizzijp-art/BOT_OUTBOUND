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


def test_bio_browser_reels_break_defaults():
    # Cadenza: dopo un numero random di profili (3-10 di default, task B.1).
    assert settings.bio_browser_reels_every_min <= settings.bio_browser_reels_every_max
    assert settings.bio_browser_reels_every_min >= 0
    # Quanti reel per pausa (2-10 di default, task B.1).
    assert settings.bio_browser_reels_count_min <= settings.bio_browser_reels_count_max
    assert settings.bio_browser_reels_count_min >= 0
    # Sosta su ciascun reel (3-10s di default, task B.1).
    assert settings.bio_browser_reels_dwell_min_s <= settings.bio_browser_reels_dwell_max_s
    assert settings.bio_browser_reels_dwell_min_s >= 0.0
    assert 0.0 <= settings.bio_browser_open_post_ratio <= 1.0


def test_bio_browser_reels_minimi_mai_zero():
    # Task B.1, diretto: i minimi erano minimi di un SORTEGGIO
    # (random.randint/uniform(min,max) in browser_bio.py/browser_import.py),
    # quindi min=0 rendeva "pausa reel disattivata" e "pausa reel di durata
    # zero uscita a caso" lo STESSO stato osservabile -- e la pausa reel
    # SOSTITUISCE quella umana (if/else), quindi una pausa reel da 0
    # profili/0 reel/0 secondi toglieva anche la pausa che ci sarebbe stata
    # (caso peggiore misurato: 0.0s per profilo, vedi
    # test_worst_case_delay_budget_su_default_odierni). Se ANCHE UNO solo di
    # questi tre minimi torna a 0, un sorteggio puo' di nuovo produrre una
    # pausa reel da 0 reel o da 0 secondi: asserire > 0 lo rende impossibile
    # a prescindere dall'esito del sorteggio, non solo nel caso medio/peggiore.
    assert settings.bio_browser_reels_every_min > 0
    assert settings.bio_browser_reels_count_min > 0
    assert settings.bio_browser_reels_dwell_min_s > 0.0
