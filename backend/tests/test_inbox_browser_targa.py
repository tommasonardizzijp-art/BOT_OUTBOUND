"""La targa provvisoria: negativa, deterministica FRA PROCESSI, senza collisioni.

Il test di determinismo gira in un SOTTOPROCESSO apposta: hash() di Python e'
randomizzato per processo (PYTHONHASHSEED), quindi un test che chiama due volte
la funzione nello stesso processo PASSA anche con un'implementazione rotta che
darebbe numeri diversi a ogni riavvio del worker.
"""
import subprocess
import sys

from app.services.inbox_browser.targa import e_provvisoria, targa_provvisoria


def test_sempre_negativa():
    for u in ("lerocchette", "modando__palermo", "a", "x" * 200):
        assert targa_provvisoria(u) < 0


def test_deterministica_nello_stesso_processo():
    assert targa_provvisoria("lerocchette") == targa_provvisoria("lerocchette")


def test_deterministica_FRA_PROCESSI():
    """La guardia vera: hash() randomizzato passerebbe il test precedente."""
    codice = (
        "import sys; sys.path.insert(0, '.');"
        "from app.services.inbox_browser.targa import targa_provvisoria;"
        "print(targa_provvisoria('lerocchette'))"
    )
    valori = set()
    for _ in range(3):
        out = subprocess.run(
            [sys.executable, "-c", codice], capture_output=True, text=True, cwd=".",
        )
        assert out.returncode == 0, out.stderr
        valori.add(out.stdout.strip())
    assert len(valori) == 1, f"targa diversa fra processi: {valori}"


def test_normalizza_maiuscole_e_chiocciola():
    """Gli username in DB hanno gia' la chiocciola su alcuni account."""
    base = targa_provvisoria("lerocchette")
    assert targa_provvisoria("LeRocchette") == base
    assert targa_provvisoria("@lerocchette") == base
    assert targa_provvisoria("  lerocchette  ") == base


def test_username_diversi_targhe_diverse():
    n = 5000
    targhe = {targa_provvisoria(f"utente_{i}") for i in range(n)}
    assert len(targhe) == n, "collisione fra targhe provvisorie"


def test_riconoscimento_provvisoria():
    assert e_provvisoria(targa_provvisoria("lerocchette")) is True
    assert e_provvisoria(76561234567) is False   # pk reale Instagram
    assert e_provvisoria(0) is False


def test_sta_nel_bigint():
    """63 bit negati: deve stare in un BIGINT firmato."""
    for i in range(2000):
        t = targa_provvisoria(f"u{i}")
        assert -(2 ** 63) < t < 0
