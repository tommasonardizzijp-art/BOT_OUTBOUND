import pytest

from app.browser.whatsapp_page import OpenResult
from app.services import wa_sender


def _ok(signal: str) -> OpenResult:
    return OpenResult(True, 1234.0, signal)


def test_invia_solo_con_cronologia_agganciata():
    esito = wa_sender.valuta_apertura(_ok("cronologia:div[data-id]:37"))
    assert esito.puo_inviare is True
    assert esito.esito_contatto is None


def test_ok_true_ma_zero_messaggi_non_invia():
    """ok=True dice solo 'composer comparso'. Zero bolle agganciate = chat
    vuota o DOM che mente: in entrambi i casi non si scrive."""
    esito = wa_sender.valuta_apertura(_ok("cronologia:div[data-id]:0"))
    assert esito.puo_inviare is False


def test_conteggio_non_parsabile_non_invia():
    """Un segnale che non si sa leggere e' un segnale che dice no."""
    esito = wa_sender.valuta_apertura(_ok("cronologia:div[data-id]:molti"))
    assert esito.puo_inviare is False
    assert esito.colpa_nostra is True


@pytest.mark.parametrize("signal,atteso", [
    ("nessuna-cronologia:nessun-messaggio-nel-pannello", "skipped"),
    ("nessuna-cronologia:sezione-chat-vuota:nessuna-conversazione-esistente", "skipped"),
    ("nessuna-cronologia:nessuna-sezione-chat:solo-gruppi-o-contatti-senza-conversazione", "skipped"),
])
def test_chat_inesistente_e_colpa_del_contatto_non_nostra(signal, atteso):
    esito = wa_sender.valuta_apertura(OpenResult(False, 1.0, signal))
    assert esito.puo_inviare is False
    assert esito.esito_contatto == atteso
    assert esito.motivo == "no_existing_chat"
    assert esito.colpa_nostra is False


@pytest.mark.parametrize("signal", [
    "nessuna-cronologia:casella-ricerca-non-trovata",
    "nessuna-cronologia:ricerca-non-svuotata",
    "nessuna-cronologia:focus-non-sulla-ricerca-pre-invio",
])
def test_guasti_nostri_non_bruciano_il_contatto(signal):
    """Un selettore rotto non deve bruciare una lista (SDD 11): il contatto
    resta queued, e' il NUMERO che si ferma."""
    esito = wa_sender.valuta_apertura(OpenResult(False, 1.0, signal))
    assert esito.puo_inviare is False
    assert esito.esito_contatto is None      # nessuna transizione di stato
    assert esito.colpa_nostra is True


def test_nessun_risultato_di_ricerca_e_ambiguo_e_non_decide_da_solo():
    """Puo' essere un numero non su WhatsApp o una ricerca rotta: chi
    chiama decide con il contesto della sessione (contratto §3.3)."""
    esito = wa_sender.valuta_apertura(
        OpenResult(False, 1.0, "nessuna-cronologia:nessun-risultato-di-ricerca"))
    assert esito.puo_inviare is False
    assert esito.esito_contatto is None
    assert esito.motivo == "ricerca_senza_risultati"
    assert esito.colpa_nostra is False


def test_segnale_sconosciuto_e_trattato_come_colpa_nostra():
    """Un segnale che il POM non produce oggi (versione futura, bug) non
    deve mai finire nel ramo 'skipped': si ferma il numero, non si brucia
    il contatto."""
    esito = wa_sender.valuta_apertura(OpenResult(False, 1.0, "boh:qualcosa-di-nuovo"))
    assert esito.puo_inviare is False
    assert esito.esito_contatto is None
    assert esito.colpa_nostra is True
