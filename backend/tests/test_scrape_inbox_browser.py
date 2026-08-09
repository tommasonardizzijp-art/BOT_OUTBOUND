"""Il motore: ciclo, stop, regola fondante.

Il test piu' importante e' quello sulla regola fondante: la sequenza "10 righe
note in cima" (i DM appena inviati) NON deve azzerare la raccolta.
"""
import pytest

from app.services.inbox_browser.riconoscimento import ArchivioNomi, ContatoreZona
from app.services.scrape_inbox_browser import decide_se_aprire


def test_una_riga_non_riconosciuta_si_apre_SEMPRE_anche_in_zona_rapida():
    """REGOLA FONDANTE. Se questo test cade, il motore raccoglie zero a regime."""
    archivio = ArchivioNomi(["Noto Uno", "Noto Due"])
    contatore = ContatoreZona()
    for _ in range(10):
        contatore.registra(True)
    assert contatore.zona == "rapida"
    assert decide_se_aprire("Sconosciuto Mai Visto", archivio, contatore.zona) is True


def test_una_riga_riconosciuta_non_si_apre():
    archivio = ArchivioNomi(["Noto Uno"])
    assert decide_se_aprire("Noto Uno", ArchivioNomi(["Noto Uno"]), "piena") is False


def test_un_segnaposto_non_si_apre_mai():
    """Profili cancellati: aprirli e' tempo perso."""
    assert decide_se_aprire("Utente Instagram", ArchivioNomi([]), "piena") is False


def test_scenario_che_affossava_il_disegno_precedente():
    """10 note in cima, poi un nuovo ogni 10: TUTTI i nuovi vanno raccolti."""
    archivio = ArchivioNomi([f"Noto {i}" for i in range(50)])
    contatore = ContatoreZona()
    aperte = 0
    for blocco in range(5):
        for i in range(9):
            nome = f"Noto {blocco * 10 + i}"
            if decide_se_aprire(nome, archivio, contatore.zona):
                aperte += 1
            contatore.registra(archivio.e_riconosciuto(nome))
        nuovo = f"Nuovo {blocco}"
        if decide_se_aprire(nuovo, archivio, contatore.zona):
            aperte += 1
        contatore.registra(archivio.e_riconosciuto(nuovo))
    assert aperte == 5, f"persi {5 - aperte} contatti nuovi su 5"
