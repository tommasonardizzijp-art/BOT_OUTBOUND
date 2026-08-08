"""Test di `cmdline_matches_profile` (_common.py, WAVE A punto A8/H2-H7).

E' l'unico pezzo di logica pura di _common.py: il resto apre un browser.
Il match deve essere sull'argomento vero (--user-data-dir=<path>), non su una
substring libera della cmdline: un domani un profilo 'profile-2' accanto a
'profile' darebbe falso positivo con un containment check, e il falso
positivo qui blocca l'avvio di una sessione legittima (_profile_in_use e'
una guardia, non un dettaglio).
"""
import pathlib
import sys

import pytest

# _common.py fa `from wa_lib import mask_pii` (import non qualificato, pensato
# per essere eseguito come script dalla sua cartella): va importato mettendo
# la cartella poc_wa stessa in sys.path, non il pacchetto poc_wa.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts" / "poc_wa"))
import _common  # noqa: E402

PROFILE = pathlib.PureWindowsPath(r"D:\wa-poc\profile")


# Le ultime due varianti valgono SOLO su Windows: cmdline_matches_profile
# normalizza con os.path.normcase/normpath, che sono deliberatamente
# dipendenti dalla piattaforma — su Linux non abbassano il case e non
# convertono '/' in '\'. Il PoC gira solo su Windows (Chromium su profilo
# persistente, cmdline con lettera di unita'), quindi la funzione e' corretta
# cosi': e' il runner CI a essere l'ambiente sbagliato per quelle due, non il
# codice a essere rotto. Saltarle li' e' scoping, non nascondere un rosso.
_SOLO_WINDOWS = pytest.mark.skipif(
    sys.platform != "win32",
    reason="normcase/normpath sono platform-dependent: equivalenza di case e "
           "di separatore vale solo su Windows, dove il PoC gira")


@pytest.mark.parametrize("cmdline", [
    ["chrome.exe", "--user-data-dir=D:\\wa-poc\\profile"],
    ["chrome.exe", '--user-data-dir="D:\\wa-poc\\profile"'],   # valore tra virgolette
    pytest.param(["chrome.exe", "--user-data-dir=D:/wa-poc/profile"],
                 marks=_SOLO_WINDOWS),                          # slash diversi
    pytest.param(["chrome.exe", "--user-data-dir=d:\\WA-POC\\PROFILE"],
                 marks=_SOLO_WINDOWS),                          # case diverso
])
def test_matches_stesso_profilo(cmdline):
    assert _common.cmdline_matches_profile(cmdline, PROFILE) is True


@pytest.mark.parametrize("cmdline", [
    ["chrome.exe", "--user-data-dir=D:\\wa-poc\\profile-2"],   # il caso del brief: NON deve matchare
    ["chrome.exe", "--user-data-dir=D:\\altro\\profile"],
    ["chrome.exe", "--user-data-dir=D:\\wa-poc\\profil"],
    ["chrome.exe"],                                            # nessun --user-data-dir
    [],
    None,
])
def test_non_matches_profilo_diverso_o_assente(cmdline):
    assert _common.cmdline_matches_profile(cmdline, PROFILE) is False


def test_substring_libera_nella_cmdline_non_basta():
    """Il path del profilo compare in cmdline ma non come valore di
    --user-data-dir=: prima della fix un containment check (`needle in cmd`)
    l'avrebbe fatto matchare per errore."""
    cmdline = ["chrome.exe", "--some-log-file=D:\\wa-poc\\profile\\debug.log"]
    assert _common.cmdline_matches_profile(cmdline, PROFILE) is False
