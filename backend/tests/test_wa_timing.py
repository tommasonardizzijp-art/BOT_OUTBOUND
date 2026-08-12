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


def test_wa_session_break_seconds_non_si_ammassa_sui_bordi(monkeypatch):
    """La pausa fra mini-sessioni non deve accumularsi sui due estremi.

    Il clamp (`max(lo, min(hi, val))`) non scarta le estrazioni fuori range: le
    schiaccia sul bound. Con i default WA (20-40 min, sigma 0.6) il 56% delle
    pause cadeva esattamente su 1200s o 2400s -- una firma peggiore di un
    ritardo costante, lo stesso difetto misurato e corretto su Instagram con
    la PR #55. Serve troncamento per RIESTRAZIONE, non bound piu' larghi.
    """
    import random

    from app.config import settings
    monkeypatch.setattr(settings, "wa_break_min_min", 20)
    monkeypatch.setattr(settings, "wa_break_max_min", 40)
    campaign = SimpleNamespace(session_min_messages=None, session_max_messages=None,
                                break_min_minutes=None, break_max_minutes=None)

    random.seed(12345)
    samples = [wa_timing.wa_session_break_seconds(campaign) for _ in range(2000)]

    lo, hi = 20 * 60.0, 40 * 60.0
    assert all(lo <= s <= hi for s in samples)
    sui_bordi = sum(1 for s in samples if s in (lo, hi))
    assert sui_bordi / len(samples) < 0.02, (
        f"{sui_bordi}/{len(samples)} pause esattamente su {lo}s o {hi}s"
    )


def test_effective_wa_active_hours_parses_HHMM_range(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "wa_active_hours", "09:30-19:30")
    campaign = SimpleNamespace(active_hours_start=None, active_hours_end=None)
    assert wa_timing.effective_wa_active_hours(campaign) == (9, 19)


def test_effective_wa_active_hours_campaign_override():
    campaign = SimpleNamespace(active_hours_start="08:00", active_hours_end="12:00")
    assert wa_timing.effective_wa_active_hours(campaign) == (8, 12)


def test_ora_locale_col_tz_database_assente_non_ignora_l_ora_legale(monkeypatch):
    """Su Windows `zoneinfo` senza il pacchetto `tzdata` solleva
    ZoneInfoNotFoundError: Windows non ha il tz database di sistema. Il
    fallback era un offset FISSO a UTC+1 (CET), che per i ~7 mesi di ora
    legale sbaglia di un'ora piena.

    Misurato in produzione il 12/08 alle 19:59 reali: il worker credeva
    fossero le 18. La finestra oraria configurata 09:00-20:00 valeva di fatto
    10:00-21:00, quindi il canale non partiva prima delle 10 e scriveva a
    clienti veri fino alle 21. La finestra oraria e' proprio la protezione che
    non deve slittare.

    L'orologio di sistema il fuso lo sa gia', ed e' lo stesso che l'operatore
    legge quando guarda la dashboard."""
    import builtins
    from datetime import datetime

    from app.workers import wa_worker

    vero_import = builtins.__import__

    def _import_senza_tzdata(name, *a, **kw):
        if name == "zoneinfo":
            raise ImportError("simula Windows senza tzdata")
        return vero_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _import_senza_tzdata)

    assert wa_worker._ora_locale_corrente() == datetime.now().hour, (
        "col tz database assente l'ora non segue piu' l'orologio di sistema: "
        "la finestra oraria slitta di un'ora durante l'ora legale")
