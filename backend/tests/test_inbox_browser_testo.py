"""Parsing e normalizzazione: la parte che sbaglia in silenzio.

Il caso peggiore dell'intero modulo e' qui: se il prefisso "Tu:" non viene
riconosciuto (perche' l'interfaccia e' in inglese), OGNI chat risulta "ha
risposto". Nessun errore, solo dati falsi.
"""
import pytest

from app.services.inbox_browser.testo import (
    LINGUE, analizza_riga_lista, e_segnaposto, estrai_ultimo_messaggio,
    estrai_username_thread, normalizza_nome,
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
