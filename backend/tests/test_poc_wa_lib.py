"""Test degli helper puri dei PoC WhatsApp (M0).

Sono gli unici pezzi di M0 testabili in isolamento: tutto il resto tocca un DOM
di terze parti. `AllowList` in particolare NON e' un dettaglio: e' la guardia che
impedisce di mandare messaggi ai contatti veri di Primero (vincolo Q60).
"""
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
from poc_wa import wa_lib  # noqa: E402


@pytest.mark.parametrize("raw,expected", [
    ("+39 342 146 0077", "393421460077"),
    ("3421460077", "393421460077"),          # manca il prefisso -> default IT
    ("0039 342 1460077", "393421460077"),
    ("+1 415 555 0123", "14155550123"),      # estero: prefisso rispettato
    ("342-146.0077", "393421460077"),
    ("", None),
    ("abc", None),
    ("+39 12", None),                        # troppo corto per essere un numero
])
def test_normalize_e164(raw, expected):
    assert wa_lib.normalize_e164(raw) == expected


@pytest.mark.parametrize("text,expected", [
    ("STOP", True),
    ("stop", True),
    ("Stop.", True),
    ("basta grazie", True),
    ("CANCELLAMI", True),
    ("non scrivermi piu", True),
    ("non voglio piu ricevere messaggi", True),
    ("stopper", False),                      # parola intera, non substring
    ("mi fermo io", False),
    ("ok grazie", False),
    ("", False),
])
def test_contains_stop(text, expected):
    assert wa_lib.contains_stop(text) is expected


def test_mask_pii_nasconde_numeri_e_tronca():
    out = wa_lib.mask_pii("chiamami al 3421460077 domani", keep=100)
    assert "3421460077" not in out
    assert "<num>" in out
    assert wa_lib.mask_pii("x" * 500) .endswith("...")
    assert len(wa_lib.mask_pii("x" * 500, keep=40)) <= 43


def test_allowlist_blocca_i_non_autorizzati(monkeypatch):
    monkeypatch.setenv("POC_WA_ALLOWED_NUMBERS", "+39 342 146 0077, 3331112222")
    al = wa_lib.AllowList.load()
    assert al.is_allowed("393421460077") is True
    assert al.is_allowed("393331112222") is True
    assert al.is_allowed("395559998888") is False
    with pytest.raises(wa_lib.NotAllowed):
        al.assert_allowed("395559998888")


def test_allowlist_vuota_blocca_tutto(monkeypatch):
    """Fail-closed: allowlist non configurata => nessun invio possibile."""
    monkeypatch.delenv("POC_WA_ALLOWED_NUMBERS", raising=False)
    al = wa_lib.AllowList.load()
    assert al.is_allowed("393421460077") is False
