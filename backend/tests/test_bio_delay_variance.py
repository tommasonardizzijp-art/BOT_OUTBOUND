"""Il delay tra bio deve avere varianza reale (lognormale con coda), non una
cadenza piatta — quella era la firma da bot segnalata (screening a intervalli
regolari con N account round-robin)."""
import statistics

from app.utils.timing import bio_fetch_delay_seconds


def test_delay_has_spread_and_tail():
    samples = [bio_fetch_delay_seconds(5.0, 8.0) for _ in range(3000)]
    # Floor rispettato.
    assert min(samples) >= 5.0
    # Coda lunga: qualche valore supera il max nominale (8) — pause "distrazione".
    assert max(samples) > 8.0
    # Cap: mai oltre 3x max.
    assert max(samples) <= 8.0 * 3.0 + 1e-6
    # Varianza reale (non una cadenza piatta): deviazione standard non banale.
    assert statistics.pstdev(samples) > 1.0
    # Non tutti uguali.
    assert len(set(round(s, 3) for s in samples)) > 100


def test_min_equals_max_still_varies():
    # Anche con min==max (config piatta) l'helper forza un range -> niente cadenza fissa.
    samples = [bio_fetch_delay_seconds(10.0, 10.0) for _ in range(1000)]
    assert min(samples) >= 10.0
    assert max(samples) > 10.0
    assert statistics.pstdev(samples) > 0.5


def test_inverted_range_is_handled():
    # max < min non deve rompere: ritorna comunque valori >= min.
    samples = [bio_fetch_delay_seconds(12.0, 6.0) for _ in range(200)]
    assert min(samples) >= 12.0
