"""Pacing tra pagine inbox: la lognormale deve essere TRONCATA, non clampata.

Col clamp la massa fuori bound si accumulava esattamente su min e max (col
vecchio 10-40 e sigma 0.9 ci finiva il 45% dei delay, 30% sul solo massimo):
due picchi netti su due valori fissi sono una firma temporale riconoscibile.
Questi test bloccano il ritorno del clamp.
"""
import statistics

import pytest

from app.config import settings
from app.services.scrape_inbox import _sample_page_delay

N = 4000


@pytest.fixture(scope="module")
def sample():
    lo = settings.inbox_api_page_delay_min_seconds
    hi = settings.inbox_api_page_delay_max_seconds
    return lo, hi, [_sample_page_delay(lo, hi) for _ in range(N)]


def test_delay_sempre_dentro_i_bound(sample):
    lo, hi, delays = sample
    assert all(lo <= d <= hi for d in delays), (
        f"fuori range: min={min(delays)} max={max(delays)} bound=[{lo},{hi}]"
    )


def test_nessuna_pila_sui_bound(sample):
    """Il difetto del clamp: >45% dei valori atterrava esattamente su lo/hi.

    Con la troncata la densita' e' liscia, quindi la frazione nell'1% estremo
    del range deve restare marginale.
    """
    lo, hi, delays = sample
    span = hi - lo
    on_lo = sum(1 for d in delays if d <= lo + span * 0.01) / N
    on_hi = sum(1 for d in delays if d >= hi - span * 0.01) / N
    assert on_lo < 0.05, f"pila sul minimo: {on_lo:.1%}"
    assert on_hi < 0.05, f"pila sul massimo: {on_hi:.1%}"


def test_varianza_ampia(sample):
    """'Molta varianza': coefficiente di variazione sopra il 30%.

    Un pacing quasi-costante e' altrettanto riconoscibile di uno fisso.
    """
    _, _, delays = sample
    cv = statistics.stdev(delays) / statistics.mean(delays)
    assert cv > 0.30, f"distribuzione troppo stretta: CV={cv:.2f}"


def test_asimmetria_lognormale(sample):
    """Coda a destra: la media sta sopra la mediana (non e' una uniforme)."""
    _, _, delays = sample
    assert statistics.mean(delays) > statistics.median(delays)


def test_copre_tutto_il_range(sample):
    """La densita' non deve concentrarsi in una sola porzione del range."""
    lo, hi, delays = sample
    mid = (lo + hi) / 2
    sotto = sum(1 for d in delays if d < mid) / N
    assert 0.10 < sotto < 0.95, f"range coperto male: {sotto:.1%} sotto la meta'"


def test_fallback_su_range_degenere():
    """lo == hi: nessuna estrazione valida possibile, deve terminare comunque."""
    assert _sample_page_delay(30, 30) == pytest.approx(30, abs=0.001)
