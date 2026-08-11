"""Il motore non deve pagare due volte la stessa riga.

Misurato l'11/08 su una sessione di 30 minuti: 271 chat uniche incontrate, ma
circa 795 righe processate, ognuna con la sua pausa. Il ciclo rilegge le righe
visibili a ogni giro e `human_click` sposta la lista a ogni apertura, quindi il
lotto successivo ricomincia in mezzo al precedente. Il 65% delle pause di
scorrimento se ne andava in righe gia' viste.
"""
from app.services.scrape_inbox_browser import gia_esaminata


def test_una_riga_mai_vista_va_esaminata():
    viste = set()
    assert gia_esaminata("bruzzo abbigliamento", viste) is False


def test_la_stessa_riga_al_giro_dopo_non_si_ripaga():
    viste = {"bruzzo abbigliamento"}
    assert gia_esaminata("bruzzo abbigliamento", viste) is True


def test_una_riga_senza_nome_non_entra_nella_memoria():
    """Senza chiave non si puo' ricordare nulla: va trattata come nuova ogni
    volta, non come 'gia' vista' — altrimenti una singola riga anonima
    zittirebbe tutte le successive."""
    viste = {""}
    assert gia_esaminata("", viste) is False
    assert gia_esaminata(None, viste) is False
