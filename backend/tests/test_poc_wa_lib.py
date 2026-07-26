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


@pytest.mark.parametrize("text", [
    "chiamami al 342 146 0077 domani",
    "342-146-0077",
    "342.146.0077",
    "+39 342 146 0077",
])
def test_mask_pii_numero_con_un_separatore(text):
    """WhatsApp mostra i numeri con separatori ("342 146 0077"), non a cifre
    consecutive: mask_pii deve mascherarli comunque (vincolo P12)."""
    out = wa_lib.mask_pii(text, keep=200)
    assert "<num>" in out
    assert not any(c.isdigit() for c in out)


@pytest.mark.parametrize("text", [
    "ore 12 30",
    "costa 15 euro",
])
def test_mask_pii_non_maschera_numeri_troppo_corti(text):
    out = wa_lib.mask_pii(text, keep=200)
    assert out == text


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


# --- load_messages (SDD wave-a-spec.md A7): blocchi separati da riga vuota ---

def test_load_messages_blocco_singolo(tmp_path):
    p = tmp_path / "messages.txt"
    p.write_text("Ciao, come stai?", encoding="utf-8")
    assert wa_lib.load_messages(p) == ["Ciao, come stai?"]


def test_load_messages_piu_blocchi(tmp_path):
    p = tmp_path / "messages.txt"
    p.write_text("Primo messaggio.\n\nSecondo messaggio.\n\nTerzo.", encoding="utf-8")
    assert wa_lib.load_messages(p) == ["Primo messaggio.", "Secondo messaggio.", "Terzo."]


def test_load_messages_a_capo_interni_conservati(tmp_path):
    """Un blocco multi-riga resta multi-riga: e' il percorso Shift+Enter di
    human_type, non deve essere schiacciato su una riga sola."""
    p = tmp_path / "messages.txt"
    p.write_text("Ciao,\ncome stai?\nTutto bene?", encoding="utf-8")
    assert wa_lib.load_messages(p) == ["Ciao,\ncome stai?\nTutto bene?"]


def test_load_messages_righe_vuote_multiple_tra_blocchi(tmp_path):
    p = tmp_path / "messages.txt"
    p.write_text("Uno\n\n\n\nDue", encoding="utf-8")
    assert wa_lib.load_messages(p) == ["Uno", "Due"]


def test_load_messages_spazi_bordo_blocco_rimossi(tmp_path):
    p = tmp_path / "messages.txt"
    p.write_text("  \nCiao,\ncome va?  \n\n", encoding="utf-8")
    assert wa_lib.load_messages(p) == ["Ciao,\ncome va?"]


def test_load_messages_bom_notepad_non_finisce_nel_testo(tmp_path):
    """Notepad salva con BOM in testa al file: i byte reali del BOM (EF BB BF)
    devono sparire, non solo il carattere \\ufeff scritto a mano."""
    p = tmp_path / "messages.txt"
    p.write_bytes(b"\xef\xbb\xbf" + "Ciao a tutti".encode("utf-8"))
    out = wa_lib.load_messages(p)
    assert out == ["Ciao a tutti"]
    assert "﻿" not in out[0]


def test_load_messages_tab_rifiutato(tmp_path):
    p = tmp_path / "messages.txt"
    p.write_text("Ciao\ta tutti", encoding="utf-8")
    with pytest.raises(wa_lib.MessagesFileError):
        wa_lib.load_messages(p)


def test_load_messages_tab_in_blocco_successivo_rifiutato(tmp_path):
    p = tmp_path / "messages.txt"
    p.write_text("Messaggio pulito\n\nMessaggio\tsporco", encoding="utf-8")
    with pytest.raises(wa_lib.MessagesFileError):
        wa_lib.load_messages(p)


def test_load_messages_file_vuoto_errore_leggibile(tmp_path):
    p = tmp_path / "messages.txt"
    p.write_text("", encoding="utf-8")
    with pytest.raises(wa_lib.MessagesFileError):
        wa_lib.load_messages(p)


def test_load_messages_file_solo_righe_vuote_errore_leggibile(tmp_path):
    p = tmp_path / "messages.txt"
    p.write_text("\n\n   \n\n", encoding="utf-8")
    with pytest.raises(wa_lib.MessagesFileError):
        wa_lib.load_messages(p)


def test_load_messages_file_assente_errore_leggibile(tmp_path):
    with pytest.raises(wa_lib.MessagesFileError):
        wa_lib.load_messages(tmp_path / "non-esiste.txt")
