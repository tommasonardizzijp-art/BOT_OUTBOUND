"""La targa provvisoria diventa vera durante l'arricchimento.

E il caso peggiore del progetto: uno username riassegnato dopo un rename farebbe
arricchire e scrivere i dati di un ESTRANEO sulla scheda sbagliata, e poi gli
manderebbe il DM. Nessun passaggio solleva un errore da solo.
"""
import pytest

from app.services.browser_bio import decidi_sostituzione_targa


def test_provvisoria_viene_sostituita():
    assert decidi_sostituzione_targa(targa_attuale=-123456, pk_vero=76561234567) == "sostituisci"


def test_targa_vera_uguale_non_si_tocca():
    assert decidi_sostituzione_targa(targa_attuale=76561234567, pk_vero=76561234567) == "invariata"


def test_targa_vera_DIVERSA_ferma_tutto():
    """Username riassegnato dopo un rename: stiamo guardando un'altra persona."""
    assert decidi_sostituzione_targa(targa_attuale=76561234567, pk_vero=99988877766) == "identita_cambiata"


def test_pk_mancante_non_sostituisce():
    assert decidi_sostituzione_targa(targa_attuale=-123456, pk_vero=None) == "invariata"


def test_pk_non_numerico_non_sostituisce():
    assert decidi_sostituzione_targa(targa_attuale=-123456, pk_vero="non_un_numero") == "invariata"
