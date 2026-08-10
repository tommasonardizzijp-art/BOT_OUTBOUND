"""Parsing e normalizzazione: la parte che sbaglia in silenzio.

Il caso peggiore dell'intero modulo e' qui: se il prefisso "Tu:" non viene
riconosciuto (perche' l'interfaccia e' in inglese), OGNI chat risulta "ha
risposto". Nessun errore, solo dati falsi.
"""
from datetime import datetime

import pytest

from app.services.inbox_browser.testo import (
    LINGUE, analizza_riga_lista, e_segnaposto, estrai_data_thread,
    estrai_ultimo_messaggio, estrai_username_thread, normalizza_nome,
)


# ── normalizzazione ────────────────────────────────────────────────────────
def test_normalizza_maiuscole_e_spazi():
    assert normalizza_nome("  Bruzzo   Abbigliamento ") == normalizza_nome("bruzzo abbigliamento")


def test_normalizza_rimuove_emoji_e_spazi_invisibili():
    assert normalizza_nome("Fashion​Style \U0001F3AF") == normalizza_nome("FashionStyle")


def test_normalizza_none_e_vuoto():
    assert normalizza_nome(None) == ""
    assert normalizza_nome("   ") == ""


# ── segnaposto multilingua ─────────────────────────────────────────────────
@pytest.mark.parametrize("nome", ["Utente Instagram", "utente instagram", "Instagram User", "INSTAGRAM USER"])
def test_segnaposto_riconosciuto_in_due_lingue(nome):
    assert e_segnaposto(nome) is True


@pytest.mark.parametrize("nome", ["Bruzzo Abbigliamento", "Patrizia Salvia", "Instagram Marketing Srl"])
def test_nome_vero_non_e_segnaposto(nome):
    assert e_segnaposto(nome) is False


# ── riga della lista ───────────────────────────────────────────────────────
def test_riga_con_prefisso_nostro_italiano():
    riga = "KIDS Mstore Civitanova Marche\nTu: Procedo con i consigli?\n22 sett"
    r = analizza_riga_lista(riga, "it")
    assert r.nome == "KIDS Mstore Civitanova Marche"
    assert r.ultimo_nostro is True
    assert r.data_relativa == "22 sett"


def test_riga_con_prefisso_nostro_inglese():
    riga = "KIDS Mstore\nYou: Shall I proceed?\n3w"
    r = analizza_riga_lista(riga, "en")
    assert r.ultimo_nostro is True


def test_riga_senza_prefisso_ha_risposto():
    riga = "Bruzzo Abbigliamento\nGrazie siamo gia' seguiti\n2 sett"
    r = analizza_riga_lista(riga, "it")
    assert r.ultimo_nostro is False


def test_LINGUA_SBAGLIATA_non_deve_mentire():
    """Il fallimento piu' insidioso: riga italiana letta come inglese.

    Deve dichiarare 'non lo so' (None), MAI 'ha risposto' (False): un False
    silenzioso classificherebbe ogni chat come risposta.
    """
    riga = "KIDS Mstore\nTu: Procedo con i consigli?\n22 sett"
    r = analizza_riga_lista(riga, "en")
    assert r.ultimo_nostro is None, "con la lingua sbagliata deve ammettere di non sapere"


def test_lingua_non_prevista_solleva():
    with pytest.raises(KeyError):
        analizza_riga_lista("qualcosa", "de")


# ── thread aperto ──────────────────────────────────────────────────────────
def test_username_thread_ignora_i_link_di_servizio():
    href = ["/reels/", "/explore/", "/claudio.abbigliamentovincente/", "/lerocchettebyelena/"]
    propri = {"claudio.abbigliamentovincente"}
    assert estrai_username_thread(href, propri) == "lerocchettebyelena"


def test_username_thread_nessun_candidato():
    assert estrai_username_thread(["/reels/", "/explore/"], set()) is None


def test_username_thread_scarta_se_ambiguo():
    """Piu' candidati = thread di gruppo o menzione: meglio nessuno che quello sbagliato."""
    href = ["/reels/", "/tizio/", "/caio/"]
    assert estrai_username_thread(href, set()) is None


def test_estrai_ultimo_messaggio_si_ferma_al_campo_di_scrittura():
    pagina = (
        "modando__palermo\nVisualizza profilo\n9 feb 2026, 20:28\n"
        "Ciao! Stavo guardando il vostro profilo.\n"
        "Grazie siamo gia' seguiti\n"
        "Scrivi un messaggio..."
    )
    assert estrai_ultimo_messaggio(pagina, "it") == "Grazie siamo gia' seguiti"


def test_estrai_ultimo_messaggio_senza_delimitatore():
    assert estrai_ultimo_messaggio("solo\nrighe\nsparse", "it") is None


def test_lo_stato_di_lettura_non_e_un_messaggio():
    """Quando l'ultimo messaggio e' nostro ed e' stato letto, Instagram scrive
    sotto la bolla 'Visualizzato 8 h fa': e' la riga che precede il campo di
    scrittura, e finiva salvata al posto del testo. Misurato il 10/08 sulla
    campagna @michele.carozza: 10 contatti su 25 raccolti avevano lo stato di
    lettura come 'ultimo messaggio', e da li' quel testo finisce nel contesto
    con cui l'AI scrive i DM."""
    pagina = (
        "modando__palermo\n9 feb 2026, 20:28\n"
        "La vostra strategia per settembre e' pronta?\n"
        "Visualizzato 8 h fa\n"
        "Scrivi un messaggio..."
    )
    assert estrai_ultimo_messaggio(pagina, "it") == "La vostra strategia per settembre e' pronta?"


def test_gli_altri_stati_di_consegna_sono_ugualmente_scartati():
    pagina = "Ciao, ci siamo sentiti a giugno\nConsegnato\nScrivi un messaggio..."
    assert estrai_ultimo_messaggio(pagina, "it") == "Ciao, ci siamo sentiti a giugno"


def test_un_messaggio_che_inizia_come_uno_stato_non_viene_scartato():
    """'Inviato' secco e' uno stato; una frase che comincia per 'Inviato' e'
    un messaggio vero. Scartarla perderebbe l'unico contenuto del thread."""
    pagina = ("Inviato il pacco ieri mattina, dovrebbe arrivare domani\n"
              "Scrivi un messaggio...")
    assert estrai_ultimo_messaggio(pagina, "it") == (
        "Inviato il pacco ieri mattina, dovrebbe arrivare domani")


def test_una_conversazione_di_soli_stati_non_inventa_un_messaggio():
    pagina = "Visualizzato 3 h fa\nScrivi un messaggio..."
    assert estrai_ultimo_messaggio(pagina, "it") is None


# ── data assoluta del thread ───────────────────────────────────────────────
def test_estrai_data_thread_italiano():
    pagina = "modando__palermo\nVisualizza profilo\n9 feb 2026, 20:28\nCiao!"
    assert estrai_data_thread(pagina, "it") == datetime(2026, 2, 9, 20, 28)


def test_estrai_data_thread_inglese():
    pagina = "modando__palermo\nView profile\n9 Feb 2026, 20:28\nHi!"
    assert estrai_data_thread(pagina, "en") == datetime(2026, 2, 9, 20, 28)


def test_estrai_data_thread_senza_data_riconoscibile():
    assert estrai_data_thread("solo\nrighe\nsparse", "it") is None


def test_estrai_data_thread_lingua_non_prevista_solleva():
    with pytest.raises(KeyError):
        estrai_data_thread("9 feb 2026, 20:28", "de")
