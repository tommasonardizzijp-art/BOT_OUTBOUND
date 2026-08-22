"""La forma di uno username Instagram reale.

Perche' un controllo sulla FORMA e non sulla lista dei segnaposto: `e_segnaposto`
(inbox_browser/testo.py) confronta con un insieme costruito da `LINGUE`, che oggi
contiene solo italiano e inglese. Il segnaposto dipende dalla lingua
dell'interfaccia dell'ACCOUNT, non da una nostra impostazione: su un account in
spagnolo o tedesco quel filtro non scatta e non lo si scopre da nessun errore.

La forma invece non dipende dalla lingua. Instagram ammette negli username solo
lettere, cifre, punto e underscore: qualunque cosa contenga uno spazio non e' un
handle, e' un nome visualizzato finito nella casella sbagliata (log reale del
22/08: `[InboxLista] @utente instagram ...`).
"""
import pytest

from app.services.inbox_browser.targa import handle_valido


@pytest.mark.parametrize("u", [
    "borderline_grow",
    "mario.rossi",
    "shop123",
    "a",
    "@conchiocciola",       # la chiocciola la toglie normalizza_username
    "  Spazi.Ai.Bordi  ",   # i bordi li toglie normalizza_username
    "MAIUSCOLO",
])
def test_handle_reale_e_valido(u):
    assert handle_valido(u) is True


@pytest.mark.parametrize("u", [
    "utente instagram",     # segnaposto IT — il caso reale del 22/08
    "instagram user",       # segnaposto EN
    "usuario de instagram",  # segnaposto ES: la lista lingue NON lo conosce, la forma si'
    "nome con spazi",
    "",
    "   ",
    None,
    "@",
    "ha-un-trattino",       # il trattino non e' ammesso da Instagram
    "ha/uno/slash",
])
def test_non_e_un_handle(u):
    assert handle_valido(u) is False


def test_lunghezza_massima():
    # Instagram si ferma a 30 caratteri: oltre, non e' un handle.
    assert handle_valido("a" * 30) is True
    assert handle_valido("a" * 31) is False


# -- Ingresso del motore inbox API -----------------------------------------

from app.services.scrape_inbox import classifica_pagina  # noqa: E402


def test_segnaposto_non_entra_in_lista():
    """Il segnaposto va scartato PRIMA di diventare una riga."""
    esito = classifica_pagina(
        participants=[(111, "borderline_grow"), (222, "utente instagram"), (333, "shop_ok")],
        existing_ids=set(),
        targa_per_username={},
    )
    nuovi = [u for _pk, u in esito.nuovi]
    assert "borderline_grow" in nuovi
    assert "shop_ok" in nuovi
    assert "utente instagram" not in nuovi
    assert esito.segnaposto_scartati == 1


def test_due_profili_chiusi_non_diventano_una_collisione():
    """Due profili chiusi condividono il segnaposto: senza il filtro sarebbero
    una falsa 'collisione username' (il warning reale del 22/08).

    `targa_per_username` DEVE contenere il segnaposto con una targa REALE
    (positiva), altrimenti il test non misura niente: il ramo che riempie
    `collisioni_username` (scrape_inbox.py) scatta solo quando lo username e' gia'
    in DB con un pk positivo. Con un dizionario vuoto quel ramo e' irraggiungibile
    a prescindere dal filtro, e l'asserzione resterebbe verde anche togliendo il
    filtro: rilievo di review del 22/08, corretto qui.
    """
    esito = classifica_pagina(
        participants=[(444, "utente instagram"), (555, "utente instagram")],
        existing_ids=set(),
        targa_per_username={"utente instagram": 333},   # un altro profilo chiuso, gia' in DB
    )
    assert esito.nuovi == []
    assert esito.collisioni_username == []
    assert esito.segnaposto_scartati == 2
