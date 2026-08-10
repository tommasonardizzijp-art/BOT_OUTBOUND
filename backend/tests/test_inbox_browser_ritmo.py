"""Il ritmo: distribuzione troncata, mai clampata, e differenziata per zona.

Il clamp non scarta la coda: la SCHIACCIA sui bound. Sul motore API ci finiva il
45% dei ritardi, su due valori fissi. Due picchi netti sono una firma piu'
riconoscibile di un ritardo costante.
"""
import statistics
from collections import Counter

import pytest

from app.services.inbox_browser.ritmo import PARAMETRI, campiona_pausa

N = 4000


@pytest.fixture(scope="module")
def campioni():
    return {z: [campiona_pausa(z) for _ in range(N)] for z in ("piena", "rapida")}


@pytest.mark.parametrize("zona", ["piena", "rapida"])
def test_dentro_i_bound(campioni, zona):
    p = PARAMETRI[zona]
    lo = min(p["normale"][0], p["sosta"][0], p["stacco"][0])
    hi = max(p["normale"][1], p["sosta"][1], p["stacco"][1])
    assert all(lo <= d <= hi for d in campioni[zona])


@pytest.mark.parametrize("zona", ["piena", "rapida"])
def test_nessuna_pila_su_un_singolo_valore(campioni, zona):
    """Il difetto del clamp: niente deve accumularsi su un valore preciso."""
    comuni = Counter(round(d, 3) for d in campioni[zona]).most_common(1)[0][1]
    assert comuni / N < 0.02, f"{comuni / N:.1%} dei valori su un unico punto"


def test_la_zona_rapida_e_davvero_piu_rapida(campioni):
    assert statistics.median(campioni["rapida"]) < statistics.median(campioni["piena"]) / 2


@pytest.mark.parametrize("zona", ["piena", "rapida"])
def test_varianza_ampia(campioni, zona):
    d = campioni[zona]
    assert statistics.stdev(d) / statistics.mean(d) > 0.30


def test_le_tre_modalita_compaiono_tutte(campioni):
    p = PARAMETRI["piena"]
    d = campioni["piena"]
    assert any(x <= p["normale"][1] for x in d)
    assert any(p["sosta"][0] <= x <= p["sosta"][1] for x in d)
    assert any(x >= p["stacco"][0] for x in d)


def test_zona_sconosciuta_solleva():
    with pytest.raises(KeyError):
        campiona_pausa("turbo")
