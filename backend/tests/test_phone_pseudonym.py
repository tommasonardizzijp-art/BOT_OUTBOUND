import pytest

from app.utils.phone_pseudonym import (PhoneNormalizationError, hmac_phone,
                                       mask_phone, normalize_e164)


@pytest.mark.parametrize("raw,atteso", [
    ("+39 342 146 0077", "393421460077"),
    ("3421460077", "393421460077"),        # nazionale italiano, prefisso implicito
    ("0039 342 146 0077", "393421460077"),
    ("+39-342-146-0077", "393421460077"),
    ("‪+393421460077‬", "393421460077"),   # marcatori Unicode dai title WhatsApp
])
def test_normalize_e164_accetta_le_forme_reali(raw, atteso):
    assert normalize_e164(raw) == atteso


@pytest.mark.parametrize("raw", ["", "   ", "abc", "+39", "12", None])
def test_normalize_e164_rifiuta_invece_di_indovinare(raw):
    """Un numero non normalizzabile e' uno SCARTO dell'ingest, non un numero
    'quasi giusto': indovinare significa scrivere a uno sconosciuto."""
    with pytest.raises(PhoneNormalizationError):
        normalize_e164(raw)


def test_hmac_e_deterministico_e_lungo_64():
    a = hmac_phone("393421460077")
    assert a == hmac_phone("393421460077")
    assert len(a) == 64 and all(c in "0123456789abcdef" for c in a)


def test_hmac_distingue_numeri_diversi():
    assert hmac_phone("393421460077") != hmac_phone("393421460078")


def test_hmac_non_contiene_il_numero():
    assert "3421460077" not in hmac_phone("393421460077")


def test_mask_mostra_solo_prefisso_e_ultime_tre():
    assert mask_phone("393421460077") == "+39•••••077"


def test_mask_non_esplode_su_numero_corto():
    """mask_phone finisce nei log degli errori: se solleva li' dentro, nasconde
    l'errore vero che stava per essere loggato."""
    assert mask_phone("39") == "+39•••••"
    assert mask_phone("") == ""
