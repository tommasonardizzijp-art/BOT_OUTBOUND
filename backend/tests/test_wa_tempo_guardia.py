"""Guardia: `datetime.utcnow()` non deve rientrare nei moduli WhatsApp.

**Perche' un test e non una nota nel piano.** La regola "il numero si cerca
per `phone_hmac`", unificata il 13/08, e' stata ignorata in buona fede dal
codice scritto il 14/08 -- non per distrazione, ma perche' niente la
difendeva. Una convenzione senza un test che la presidia viene riaperta dal
primo che arriva, e questa in particolare non lascia tracce visibili: il
codice sbagliato passa la code review, passa la suite (che gira su SQLite) e
sbaglia solo in produzione, di due ore, in silenzio.

Il difetto e' spiegato per esteso in `app/utils/tempo.py`.

**Perche' AST e non una regex.** Un commento o una docstring che *citano*
`utcnow` per spiegare il difetto non sono violazioni -- ce ne sono gia'
diversi, scritti apposta. L'albero sintattico non contiene i commenti e
tratta le docstring come stringhe, quindi la distinzione viene gratis e non
va inseguita a mano.
"""
import ast
from pathlib import Path

RADICE = Path(__file__).resolve().parents[1]

# I moduli dove il difetto costa: scrivono o confrontano colonne
# DateTime(timezone=True) del dominio WhatsApp. `tenant.py` e' dentro perche'
# `tenants.created_at` e' timestamptz e nasce dallo stesso default.
PATTERN_SORVEGLIATI = (
    "app/services/wa_*.py",
    "app/services/wa_*/**/*.py",
    "app/workers/wa_*.py",
    "app/workers/cron_worker.py",
    "app/models/wa.py",
    "app/models/tenant.py",
    "app/api/wa_*.py",
)


def moduli_sorvegliati() -> list[Path]:
    trovati: set[Path] = set()
    for pattern in PATTERN_SORVEGLIATI:
        trovati.update(p for p in RADICE.glob(pattern) if p.is_file())
    return sorted(trovati)


def _violazioni(sorgente: str) -> list[tuple[int, str]]:
    """Righe che *eseguono* `utcnow`, in qualunque forma raggiungibile.

    Tre forme, perche' tutte e tre esistono gia' nel repo o sono a un passo:
    `datetime.utcnow()`, `_dt.utcnow()` (alias di modulo, in wa_worker) e un
    ipotetico `utcnow()` importato nudo. Si guarda il *riferimento* e non la
    chiamata: `default=datetime.utcnow` nei modelli non e' una chiamata, ed e'
    proprio il caso piu' diffuso.
    """
    albero = ast.parse(sorgente)
    colpe: list[tuple[int, str]] = []
    for nodo in ast.walk(albero):
        if isinstance(nodo, ast.Attribute) and nodo.attr == "utcnow":
            base = getattr(nodo.value, "id", None) or getattr(nodo.value, "attr", "?")
            colpe.append((nodo.lineno, f"{base}.utcnow"))
        elif isinstance(nodo, ast.Name) and nodo.id == "utcnow":
            colpe.append((nodo.lineno, "utcnow (importato nudo)"))
        elif isinstance(nodo, ast.ImportFrom):
            for alias in nodo.names:
                if alias.name == "utcnow":
                    colpe.append((nodo.lineno, "from ... import utcnow"))
    return colpe


def test_nessun_utcnow_nei_moduli_whatsapp():
    moduli = moduli_sorvegliati()
    # Se la lista si svuota (rename di cartella, glob che non matcha piu') il
    # test passerebbe senza guardare niente: e' il modo classico in cui una
    # guardia muore in silenzio.
    assert len(moduli) >= 25, (
        f"la guardia sorveglia solo {len(moduli)} file: i pattern non stanno "
        "piu' trovando i moduli WhatsApp")

    colpevoli: list[str] = []
    for modulo in moduli:
        for riga, forma in _violazioni(modulo.read_text(encoding="utf-8")):
            rel = modulo.relative_to(RADICE).as_posix()
            colpevoli.append(f"{rel}:{riga} -> {forma}")

    assert not colpevoli, (
        f"{len(colpevoli)} usi di utcnow() nei moduli WhatsApp. E' naive: "
        "scritto in una colonna timestamptz finisce a DB spostato dell'offset "
        "del fuso (-2h in ora legale). Usa app.utils.tempo.adesso_utc().\n  "
        + "\n  ".join(colpevoli))


def test_la_guardia_distingue_i_commenti_dalle_chiamate():
    """Prova del nove della guardia stessa: se bocciasse anche i commenti,
    il test sopra sarebbe inservibile (il codice corretto e' pieno di commenti
    che spiegano perche' utcnow non si usa) e verrebbe disattivato al primo
    fastidio."""
    innocente = '''
"""Docstring che cita datetime.utcnow() per spiegare il difetto."""
# Anche questo commento nomina datetime.utcnow().
MESSAGGIO = "usa adesso_utc, non datetime.utcnow()"
'''
    assert _violazioni(innocente) == []
    assert _violazioni("import datetime\nx = datetime.datetime.utcnow()\n") == [
        (2, "datetime.utcnow")]
