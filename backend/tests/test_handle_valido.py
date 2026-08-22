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
