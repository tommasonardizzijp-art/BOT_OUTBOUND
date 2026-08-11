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


# ── il ritmo dello SCORRIMENTO non e' quello delle azioni ──────────────────
# Misurato l'11/08 su una sessione supervisionata di 18 minuti: il 91% del
# tempo era `sleep`, e dentro quel 91% circa tre quarti erano "stacchi" da 2-5
# minuti. La causa non e' il valore dello stacco: e' che la pausa veniva presa
# a OGNI riga esaminata, comprese le 144 su 170 che il motore non ha aperto
# perche' gia' note. Su quelle righe non parte nessuna richiesta verso
# Instagram: dormirci sopra non riduce il footprint di un byte, riduce solo il
# throughput. Fermarsi cinque minuti dopo aver scorso cento righe non e'
# nemmeno umano.
from app.services.inbox_browser.ritmo import zona_pausa   # noqa: E402


def test_scorrere_righe_note_non_e_un_azione_verso_instagram():
    assert zona_pausa("rapida", ha_aperto=False) == "scorrimento"
    assert zona_pausa("piena", ha_aperto=False) == "scorrimento"


def test_aprire_una_chat_mantiene_il_ritmo_della_zona():
    """L'apertura e' l'unica cosa che Instagram vede davvero: li' il ritmo
    completo, stacchi compresi, resta intatto."""
    assert zona_pausa("rapida", ha_aperto=True) == "rapida"
    assert zona_pausa("piena", ha_aperto=True) == "piena"


def test_lo_scorrimento_non_produce_mai_stacchi_da_minuti():
    campioni = [campiona_pausa("scorrimento") for _ in range(N)]
    assert max(campioni) < 60, f"pausa da {max(campioni):.0f}s durante il solo scorrimento"


def test_lo_scorrimento_conserva_qualche_sosta_breve():
    """Non deve diventare un metronomo: una pausa occasionale da una decina di
    secondi resta, e' quella che fa un umano che si sofferma su un nome."""
    campioni = [campiona_pausa("scorrimento") for _ in range(N)]
    soste = [d for d in campioni if d >= 5]
    assert soste, "nessuna sosta: il ritmo diventa piatto"
    assert len(soste) / N < 0.10, "troppe soste: tanto vale tenere il ritmo pieno"


def test_scorrere_costa_circa_un_secondo_a_riga():
    """Il conto che giustifica tutto il cambiamento.

    Sulla sessione dell'11/08 una riga costava in media 5.7s, anche quando era
    solo scorsa: 170 righe = 16 minuti di sleep. Sotto 1.2s a riga, le stesse
    170 righe stanno in tre minuti scarsi.

    La soglia e' assoluta di proposito: confrontare due estrazioni casuali —
    una delle quali contiene stacchi da 2-5 minuti che escono il 2% delle
    volte — da' un test che passa o fallisce a seconda della fortuna, non del
    codice.
    """
    media = statistics.mean(campiona_pausa("scorrimento") for _ in range(N))
    assert media < 1.2, f"{media:.2f}s a riga: 170 righe sarebbero {media * 170 / 60:.1f} min"


# ── gli stacchi sulle aperture (Task 11, via libera esplicito di Tommaso) ──
def test_uno_stacco_non_puo_valere_un_quarto_della_sessione():
    """Misurato l'11/08: 2 stacchi su 98 aperture pesavano 420s su 1727s totali.
    Il costo atteso di uno stacco per apertura deve stare sotto i 2 secondi,
    altrimenti la coda governa la media."""
    p = PARAMETRI["piena"]
    costo_atteso = p["p_stacco"] * (p["stacco"][0] + p["stacco"][1]) / 2
    assert costo_atteso < 2.0, f"{costo_atteso:.1f}s per apertura solo di stacchi"
