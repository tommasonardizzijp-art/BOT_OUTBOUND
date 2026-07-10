"""Test deterministici di DmBatchPacer: il feed browse scatta dopo un batch di
1-4 DM riusciti, non dopo ogni DM. rng iniettato -> zero flakiness."""
import pytest

from app.utils.dm_batch import DmBatchPacer


class _ScriptedRng:
    """randint() restituisce valori scriptati in sequenza (e valida i bound)."""

    def __init__(self, values):
        self._values = list(values)
        self._i = 0
        self.calls = 0

    def randint(self, a, b):
        self.calls += 1
        v = self._values[self._i % len(self._values)]
        self._i += 1
        assert a <= v <= b, f"scripted {v} fuori da [{a},{b}]"
        return v


def _simulate(pacer, n_dms):
    """Mima l'ordine del worker: PRIMA il gate (browse se il batch e' completo),
    POI l'invio. Ritorna (batch_sizes, resto) dove batch_sizes sono le dimensioni
    dei batch chiusi da un browse."""
    batch_sizes = []
    current = 0
    for _ in range(n_dms):
        if pacer.should_browse():          # step 9: gate prima dell'invio
            batch_sizes.append(current)
            pacer.record_browse()
            current = 0
        pacer.record_sent()                # invio riuscito
        current += 1
    return batch_sizes, current


def test_target_iniziale_dal_rng_e_stato_pulito():
    p = DmBatchPacer(1, 4, _ScriptedRng([3]))
    assert p.target == 3
    assert p.sent_in_batch == 0
    assert p.should_browse() is False  # 0 >= 3 -> no


def test_browse_solo_dopo_esattamente_target_dm():
    p = DmBatchPacer(1, 4, _ScriptedRng([3]))
    p.record_sent(); assert p.should_browse() is False  # 1
    p.record_sent(); assert p.should_browse() is False  # 2
    p.record_sent(); assert p.should_browse() is True   # 3 == target


def test_record_browse_resetta_e_ripesca_target():
    rng = _ScriptedRng([2, 4])
    p = DmBatchPacer(1, 4, rng)
    assert p.target == 2
    p.record_sent(); p.record_sent()
    assert p.should_browse() is True
    p.record_browse()
    assert p.sent_in_batch == 0
    assert p.target == 4            # ripescato dal rng
    assert p.should_browse() is False


def test_solo_i_sent_contano_no_auto_avanzamento():
    p = DmBatchPacer(1, 4, _ScriptedRng([2]))
    # senza record_sent lo stato non avanza da solo
    for _ in range(5):
        assert p.should_browse() is False
    p.record_sent(); p.record_sent()
    assert p.should_browse() is True


def test_reset_azzera_il_batch():
    p = DmBatchPacer(1, 4, _ScriptedRng([3, 2]))
    p.record_sent(); p.record_sent()
    p.reset()
    assert p.sent_in_batch == 0
    assert p.target == 2
    assert p.should_browse() is False


def test_cadenza_completa_batch_2_1_3():
    """Con target scriptati 2,1,3 i browse chiudono batch di 2, poi 1, poi 3."""
    p = DmBatchPacer(1, 4, _ScriptedRng([2, 1, 3, 4]))
    batch_sizes, _ = _simulate(p, 10)
    assert batch_sizes[:3] == [2, 1, 3]


def test_batch_min_1_scrolla_ogni_dm():
    """min=max=1 -> browse dopo ogni singolo DM (caso degenere = comportamento vecchio)."""
    p = DmBatchPacer(1, 1, _ScriptedRng([1]))
    batch_sizes, _ = _simulate(p, 5)
    assert batch_sizes == [1, 1, 1, 1]  # ogni DM chiude un batch


@pytest.mark.parametrize("bmin,bmax", [(1, 4), (2, 2), (1, 1), (3, 4)])
def test_target_sempre_nei_bound_con_random_vero(bmin, bmax):
    import random
    p = DmBatchPacer(bmin, bmax, random)
    for _ in range(500):
        assert bmin <= p.target <= bmax
        p.record_browse()


def test_bound_difensivi_config_sballata():
    # min < 1 -> clamp a 1 ; max < min -> clamp a min
    p1 = DmBatchPacer(0, 0, _ScriptedRng([1]))
    assert p1.target == 1 and p1._min == 1 and p1._max == 1
    p2 = DmBatchPacer(5, 2, _ScriptedRng([5]))
    assert p2._min == 5 and p2._max == 5
