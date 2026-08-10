"""Riconoscimento per nome e contatore di zona.

Il difetto che questi test proteggono e' quello che affossava il disegno
precedente: in cima alla lista ci sono i DM appena inviati (tutti noti), quindi
il contatore va subito a 10. Se da quel momento si smettesse di aprire, il motore
raccoglierebbe ZERO a regime.
"""
from app.services.inbox_browser.riconoscimento import ArchivioNomi, ContatoreZona


def test_nome_presente_e_riconosciuto():
    a = ArchivioNomi(["Bruzzo Abbigliamento", "Patrizia Salvia"])
    assert a.e_riconosciuto("bruzzo  abbigliamento") is True


def test_nome_assente_non_e_riconosciuto():
    a = ArchivioNomi(["Bruzzo Abbigliamento"])
    assert a.e_riconosciuto("Max Fashion") is False


def test_nome_ambiguo_non_vale_come_riconoscimento():
    """Due schede con lo stesso nome: non possiamo sapere quale sia."""
    a = ArchivioNomi(["Fashion Style", "Fashion Style", "Bruzzo"])
    assert a.e_riconosciuto("Fashion Style") is False


def test_nomi_vuoti_non_creano_un_falso_riconoscimento():
    """I contatti raccolti via API hanno full_name=None: se collassassero tutti
    sulla stringa vuota, una riga senza nome risulterebbe 'nota'."""
    a = ArchivioNomi([None, None, None, "Bruzzo"])
    assert a.e_riconosciuto(None) is False
    assert a.e_riconosciuto("") is False


def test_aggiungi_stesso_nome_lo_rende_ambiguo():
    """aggiungi() deve mantenere la stessa invariante del costruttore: se un
    secondo profilo con lo stesso nome viene raccolto in seguito, il nome non
    e' piu' univoco e smette di valere come riconoscimento."""
    a = ArchivioNomi(["Mario Rossi"])
    assert a.e_riconosciuto("Mario Rossi") is True
    a.aggiungi("Mario Rossi")
    assert a.e_riconosciuto("Mario Rossi") is False


def test_segnaposto_mai_riconosciuto():
    a = ArchivioNomi(["Utente Instagram", "Bruzzo"])
    assert a.e_riconosciuto("Utente Instagram") is False


def test_parte_in_zona_piena():
    assert ContatoreZona().zona == "piena"


def test_dieci_riconosciuti_passano_a_rapida():
    c = ContatoreZona()
    for _ in range(9):
        assert c.registra(True) == "piena"
    assert c.registra(True) == "rapida"


def test_un_solo_non_riconosciuto_azzera_il_contatore():
    c = ContatoreZona()
    for _ in range(9):
        c.registra(True)
    c.registra(False)
    for _ in range(9):
        assert c.registra(True) == "piena"


def test_tre_sconosciuti_su_dieci_tornano_a_piena():
    c = ContatoreZona()
    for _ in range(10):
        c.registra(True)
    assert c.zona == "rapida"
    c.registra(False)
    c.registra(True)
    c.registra(False)
    assert c.registra(False) == "piena"


def test_due_sconosciuti_su_dieci_restano_in_rapida():
    c = ContatoreZona()
    for _ in range(10):
        c.registra(True)
    c.registra(False)
    for _ in range(8):
        c.registra(True)
    assert c.registra(False) == "rapida"


def test_la_zona_non_decide_se_aprire():
    """Guardia sull'invariante piu' importante del modulo.

    ContatoreZona NON deve esporre nessun metodo del tipo 'devo aprire?': la
    regola fondante e' che una riga non riconosciuta si apre SEMPRE. Se qualcuno
    aggiunge quel metodo, sta reintroducendo il difetto che azzerava la raccolta.
    """
    metodi = {m for m in dir(ContatoreZona) if not m.startswith("_")}
    vietati = {"deve_aprire", "salta", "apre", "skip"}
    assert not (metodi & vietati), f"la zona non decide le aperture: {metodi & vietati}"
