"""Test dello stato persistente dei PoC WhatsApp (M0): opt-out + memoria invii.

Logica pura + file JSON: e' l'unico pezzo di questa wave testabile senza un
browser (come wa_lib), quindi si scrive in TDD.

Tutti i test usano tmp_path di pytest: MAI D:\\wa-poc, che e' lo stato reale
del PoC in corso.
"""
import json
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
from poc_wa import poc_state  # noqa: E402


# ---------------------------------------------------------------------------
# OptOutStore
# ---------------------------------------------------------------------------

def test_optout_store_assente_e_insieme_vuoto(tmp_path):
    """Primo run: il file non esiste ancora. Non e' un errore, e' 'nessuno opted out'."""
    path = tmp_path / "optout.json"
    assert not path.exists()
    store = poc_state.OptOutStore.load(path)
    assert store.is_opted_out("393421460077") is False


def test_optout_add_poi_is_opted_out(tmp_path):
    path = tmp_path / "optout.json"
    store = poc_state.OptOutStore.load(path)
    store.add("393421460077", motivo="STOP nella coda DOM")
    assert store.is_opted_out("393421460077") is True


def test_optout_persiste_tra_due_load(tmp_path):
    """Il senso del modulo: un opt-out scritto in un run deve essere visibile
    in un run successivo (processo nuovo, load da zero)."""
    path = tmp_path / "optout.json"
    store1 = poc_state.OptOutStore.load(path)
    store1.add("393421460077", motivo="STOP")

    store2 = poc_state.OptOutStore.load(path)
    assert store2.is_opted_out("393421460077") is True


def test_optout_numero_diverso_non_bloccato(tmp_path):
    path = tmp_path / "optout.json"
    store = poc_state.OptOutStore.load(path)
    store.add("393421460077", motivo="STOP")
    assert store.is_opted_out("393331112222") is False


def test_optout_file_corrotto_solleva_errore_leggibile(tmp_path):
    """Un file corrotto NON deve essere trattato come vuoto in silenzio: sarebbe
    il modo peggiore di perdere opt-out registrati (si scriverebbe a chi aveva
    chiesto di smettere)."""
    path = tmp_path / "optout.json"
    path.write_text("{questo non e' json valido", encoding="utf-8")
    with pytest.raises(poc_state.PocStateCorrupted):
        poc_state.OptOutStore.load(path)


def test_optout_scrittura_atomica_json_valido(tmp_path):
    """Dopo add() il file su disco e' un JSON leggibile (niente scrittura a metà)."""
    path = tmp_path / "optout.json"
    store = poc_state.OptOutStore.load(path)
    store.add("393421460077", motivo="STOP")

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    assert "393421460077" in data
    assert data["393421460077"]["motivo"] == "STOP"


def test_optout_due_store_indipendenti_non_si_sovrascrivono(tmp_path):
    """Due OptOutStore caricati ENTRAMBI prima di qualunque scrittura (come due
    run/processi che partono nello stesso istante): se add() serializza solo la
    copia in memoria caricata all'avvio, il secondo write sovrascrive il primo
    e un opt-out registrato sparisce. add() deve rileggere lo stato da disco e
    fondersi con esso, non con last-write-wins."""
    path = tmp_path / "optout.json"
    store1 = poc_state.OptOutStore.load(path)
    store2 = poc_state.OptOutStore.load(path)  # letto prima che store1 scriva

    store1.add("393421460077", motivo="STOP")
    store2.add("393331112222", motivo="basta")

    ricaricato = poc_state.OptOutStore.load(path)
    assert ricaricato.is_opted_out("393421460077") is True
    assert ricaricato.is_opted_out("393331112222") is True


def test_optout_nessun_file_temporaneo_residuo(tmp_path):
    """La scrittura atomica non deve lasciare file .tmp nella directory."""
    path = tmp_path / "optout.json"
    store = poc_state.OptOutStore.load(path)
    store.add("393421460077", motivo="STOP")
    leftovers = [p for p in tmp_path.iterdir() if p != path]
    assert leftovers == []


# ---------------------------------------------------------------------------
# SentLog
# ---------------------------------------------------------------------------

def test_sentlog_assente_e_insieme_vuoto(tmp_path):
    path = tmp_path / "sent_log.json"
    log = poc_state.SentLog.load(path)
    assert log.already_sent("393421460077", "Ciao, come va?") is False


def test_sentlog_record_poi_already_sent(tmp_path):
    path = tmp_path / "sent_log.json"
    log = poc_state.SentLog.load(path)
    log.record("393421460077", "Ciao, come va?")
    assert log.already_sent("393421460077", "Ciao, come va?") is True


def test_sentlog_persiste_tra_due_load(tmp_path):
    path = tmp_path / "sent_log.json"
    log1 = poc_state.SentLog.load(path)
    log1.record("393421460077", "Ciao, come va?")

    log2 = poc_state.SentLog.load(path)
    assert log2.already_sent("393421460077", "Ciao, come va?") is True


def test_sentlog_stesso_testo_a_destinatari_diversi_non_bloccato(tmp_path):
    """Lo stesso template mandato a due persone diverse non e' 'gia' mandato'."""
    path = tmp_path / "sent_log.json"
    log = poc_state.SentLog.load(path)
    log.record("393421460077", "Ciao, come va?")
    assert log.already_sent("393331112222", "Ciao, come va?") is False


def test_sentlog_testo_diverso_stesso_destinatario_non_bloccato(tmp_path):
    path = tmp_path / "sent_log.json"
    log = poc_state.SentLog.load(path)
    log.record("393421460077", "Ciao, come va?")
    assert log.already_sent("393421460077", "Buongiorno!") is False


def test_sentlog_non_salva_il_testo_in_chiaro(tmp_path):
    """Vincolo esplicito: i testi restano su disco per settimane, mai in chiaro."""
    path = tmp_path / "sent_log.json"
    log = poc_state.SentLog.load(path)
    testo = "Ciao, questo e' un messaggio molto riconoscibile"
    log.record("393421460077", testo)

    raw = path.read_text(encoding="utf-8")
    assert testo not in raw


def test_sentlog_file_corrotto_solleva_errore_leggibile(tmp_path):
    path = tmp_path / "sent_log.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")  # JSON valido ma non l'oggetto atteso
    with pytest.raises(poc_state.PocStateCorrupted):
        poc_state.SentLog.load(path)


def test_sentlog_scrittura_atomica_json_valido(tmp_path):
    path = tmp_path / "sent_log.json"
    log = poc_state.SentLog.load(path)
    log.record("393421460077", "Ciao, come va?")

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    assert "393421460077" in data
    assert isinstance(data["393421460077"], list)
    assert len(data["393421460077"][0]) == 64  # sha256 esadecimale


def test_sentlog_due_log_indipendenti_non_si_sovrascrivono(tmp_path):
    """Stesso bug del test analogo su OptOutStore, versione SentLog: due
    istanze caricate entrambe prima di qualunque record() non devono farsi
    last-write-wins a vicenda."""
    path = tmp_path / "sent_log.json"
    log1 = poc_state.SentLog.load(path)
    log2 = poc_state.SentLog.load(path)

    log1.record("393421460077", "Ciao, come va?")
    log2.record("393331112222", "Buongiorno!")

    ricaricato = poc_state.SentLog.load(path)
    assert ricaricato.already_sent("393421460077", "Ciao, come va?") is True
    assert ricaricato.already_sent("393331112222", "Buongiorno!") is True


def test_sentlog_record_ripetuto_non_duplica(tmp_path):
    """record() due volte con lo stesso testo non deve far crescere il file
    all'infinito (idempotenza)."""
    path = tmp_path / "sent_log.json"
    log = poc_state.SentLog.load(path)
    log.record("393421460077", "Ciao, come va?")
    log.record("393421460077", "Ciao, come va?")

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    assert len(data["393421460077"]) == 1
