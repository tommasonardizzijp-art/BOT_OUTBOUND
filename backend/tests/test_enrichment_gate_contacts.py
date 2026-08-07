"""/info/ e' l'unica richiesta che nessuna pagina web produce mai: parte solo
quando la campagna ha davvero chiesto i contatti.

Senza questo gate il livello 'bio' pagherebbe il rischio dei contatti senza
volerli — cioe' il difetto che i livelli esistono per eliminare.
"""
from types import SimpleNamespace

import pytest

from app.services.browser_bio import contatti_richiesti


def _campagna(livello):
    return SimpleNamespace(enrichment_level=livello)


def test_solo_contacts_chiede_i_contatti(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "bio_browser_contact_info_enabled", True)

    assert contatti_richiesti(_campagna("contacts")) is True
    assert contatti_richiesti(_campagna("bio")) is False
    assert contatti_richiesti(_campagna("none")) is False


def test_il_killswitch_globale_vince_su_tutto(monkeypatch):
    # Se l'interruttore globale e' spento, nessun livello puo' riaccenderlo.
    from app.config import settings
    monkeypatch.setattr(settings, "bio_browser_contact_info_enabled", False)

    for livello in ("none", "bio", "contacts"):
        assert contatti_richiesti(_campagna(livello)) is False


def test_campagna_senza_il_campo_si_comporta_come_prima(monkeypatch):
    # Retrocompatibilita': prima del livello, con l'interruttore acceso, /info/
    # partiva sempre. Una campagna senza il campo non deve regredire in silenzio.
    from app.config import settings
    monkeypatch.setattr(settings, "bio_browser_contact_info_enabled", True)

    assert contatti_richiesti(SimpleNamespace()) is True
