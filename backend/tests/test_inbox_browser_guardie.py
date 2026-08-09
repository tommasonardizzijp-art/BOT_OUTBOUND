"""Difesa in profondita': l'anagrafica globale rifiuta le targhe provvisorie.

Se una targa provvisoria arrivasse in GlobalContact, la protezione anti-doppio-DM
cross-campagna non riconoscerebbe la persona (chiave diversa da quella registrata
via API) e potrebbe mandarle un secondo messaggio.
"""
import pytest

from app.services.global_contact_service import targa_ammessa_in_anagrafica


def test_targa_vera_ammessa():
    assert targa_ammessa_in_anagrafica(76561234567) is True


def test_targa_provvisoria_rifiutata():
    assert targa_ammessa_in_anagrafica(-8834567123) is False


def test_zero_e_none_rifiutati():
    assert targa_ammessa_in_anagrafica(0) is False
    assert targa_ammessa_in_anagrafica(None) is False
