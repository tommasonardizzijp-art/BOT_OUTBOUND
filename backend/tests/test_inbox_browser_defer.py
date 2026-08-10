# backend/tests/test_inbox_browser_defer.py
"""Se il motore cambia mentre la sessione gira, si esce subito e pulito."""
from types import SimpleNamespace

from app.services.scrape_inbox_browser import motore_ancora_nostro


def test_motore_invariato_prosegue():
    assert motore_ancora_nostro(SimpleNamespace(inbox_engine="browser")) is True


def test_motore_cambiato_interrompe():
    assert motore_ancora_nostro(SimpleNamespace(inbox_engine="api")) is False


def test_campo_assente_interrompe():
    """Default 'api': se il campo sparisce non siamo piu' noi a dover girare."""
    assert motore_ancora_nostro(SimpleNamespace()) is False
