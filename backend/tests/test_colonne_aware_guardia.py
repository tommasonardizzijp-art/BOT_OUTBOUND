"""Guardia: nessuna colonna aware fuori dai modelli gia' convertiti.

**Il difetto che questo test impedisce, e perche' non e' teorico.**
Il censimento del 17/08 ha contato **358 `datetime.utcnow()` in 113 file** fuori
dal canale WhatsApp. Oggi sono tutte innocue per un motivo solo: nessuna colonna
di quei modelli e' `DateTime(timezone=True)`, quindi naive dentro e naive fuori
si annullano. Non sono state convertite proprio per questo -- toccare 113 file
di codice in produzione per un difetto che non esiste e' un rischio senza
contropartita.

Quel "oggi sono innocue" pero' non e' una proprieta' del codice: e' una
coincidenza fra due file distinti, i modelli e i servizi. Basta che qualcuno
aggiunga `timezone=True` a una colonna Instagram -- una riga, in un altro file,
per un motivo del tutto ragionevole -- perche' decine di punti comincino a
sbagliare **insieme**, e nel modo peggiore: senza eccezioni, senza log, con le
date spostate di due ore. E' esattamente com'e' andata su WhatsApp, dove il
conto e' stato di 4.720 righe da migrare.

I punti che farebbero piu' male sono gia' mappati in
`progetti/bot-outbound/utcnow-instagram-censimento` (nel second brain): sei
`update()`/`delete()` ORM con filtro temporale -- `account_lease.acquire()`, i due
`release_stale_locks` su `Follower.locked_at`, `reservation.try_reserve()`, le due
implementazioni gemelle del cooldown -- che con `synchronize_session` di default
rivalutano il `WHERE` **in Python**, ed e' li' che il 16/08 e' saltato fuori il
`TypeError`.

**Cosa fare quando questo test diventa rosso.** Non e' un divieto di aggiungere
colonne aware: e' un promemoria che quella riga ha un prerequisito. Prima si
convertono a `app.utils.tempo.adesso_utc()` le `utcnow()` dei servizi che
scrivono o confrontano quella colonna, poi si aggiunge il file a `CONVERTITI` qui
sotto. L'ordine inverso e' il bug.
"""
import ast
from pathlib import Path

RADICE = Path(__file__).resolve().parents[1]
MODELLI = RADICE / "app" / "models"

# I moduli le cui `utcnow()` sono gia' state bonificate (cutover del 16/08) e che
# quindi possono avere colonne aware. Aggiungere un nome qui e' una dichiarazione:
# "i servizi che toccano queste colonne usano adesso_utc()".
CONVERTITI = {"wa.py", "tenant.py"}

# Entrambi i tipi SQLAlchemy che portano il fuso. TIMESTAMP non e' usato oggi nel
# repo ed e' incluso apposta: il difetto deve restare coperto anche se domani
# qualcuno preferisce quello.
TIPI_TEMPORALI = {"DateTime", "TIMESTAMP"}


def _colonne_aware(sorgente: str) -> list[tuple[int, str]]:
    """Righe che dichiarano un tipo temporale CON fuso.

    Si guarda la chiamata al tipo (`DateTime(timezone=True)`) e non il
    `mapped_column` che la avvolge: la stessa dichiarazione compare come
    `mapped_column(...)`, `Column(...)` o dentro un `alembic` op, e il tipo e'
    l'unica forma comune a tutte.
    """
    trovate: list[tuple[int, str]] = []
    for nodo in ast.walk(ast.parse(sorgente)):
        if not isinstance(nodo, ast.Call):
            continue
        nome = getattr(nodo.func, "id", None) or getattr(nodo.func, "attr", None)
        if nome not in TIPI_TEMPORALI:
            continue
        for kw in nodo.keywords:
            if (kw.arg == "timezone"
                    and isinstance(kw.value, ast.Constant) and kw.value.value is True):
                trovate.append((nodo.lineno, f"{nome}(timezone=True)"))
    return trovate


def test_nessuna_colonna_aware_fuori_dai_modelli_convertiti():
    modelli = sorted(p for p in MODELLI.glob("*.py") if p.name != "__init__.py")
    assert len(modelli) >= 12, (
        f"la guardia vede solo {len(modelli)} modelli: il glob non sta piu' "
        "trovando app/models/, e passerebbe senza guardare niente")

    fuori: list[str] = []
    for modello in modelli:
        if modello.name in CONVERTITI:
            continue
        for riga, forma in _colonne_aware(modello.read_text(encoding="utf-8")):
            fuori.append(f"app/models/{modello.name}:{riga} -> {forma}")

    assert not fuori, (
        f"{len(fuori)} colonne aware in modelli le cui `utcnow()` NON sono state "
        "convertite. Da questo momento ogni datetime.utcnow() che scrive o "
        "confronta quelle colonne sbaglia di due ore, in silenzio e senza "
        "eccezioni (358 occorrenze censite in 113 file, mappa in "
        "progetti/bot-outbound/utcnow-instagram-censimento nel second brain).\n"
        "Prima converti a app.utils.tempo.adesso_utc() i servizi che toccano "
        "quella colonna -- partendo dagli update()/delete() ORM con filtro "
        "temporale, che rivalutano il WHERE in Python -- poi aggiungi il file a "
        "CONVERTITI qui sopra.\n  " + "\n  ".join(fuori))


def test_i_modelli_convertiti_hanno_davvero_colonne_aware():
    """Ancoraggio del rilevatore. Se `_colonne_aware` smettesse di riconoscere la
    forma usata nel repo -- un refactor a `TIMESTAMP`, un alias nuovo, un cambio
    di AST fra versioni di Python -- il test sopra diventerebbe verde per sempre
    senza guardare piu' niente. Qui si pretende che trovi cio' che sappiamo
    esserci: 18 colonne in wa.py e 1 in tenant.py al 17/08."""
    for nome, minimo in (("wa.py", 15), ("tenant.py", 1)):
        trovate = _colonne_aware((MODELLI / nome).read_text(encoding="utf-8"))
        assert len(trovate) >= minimo, (
            f"il rilevatore trova solo {len(trovate)} colonne aware in {nome}, "
            f"ne servono almeno {minimo}: non riconosce piu' la forma con cui "
            "sono dichiarate, quindi non le troverebbe nemmeno altrove")


def test_la_guardia_distingue_aware_da_naive():
    """Prova del nove sul rilevatore, nei due versi: deve vedere la colonna
    aware e NON deve accusare quella naive, che e' la forma corretta e diffusa
    ovunque nel repo. Un rilevatore che accusa tutto verrebbe disattivato."""
    aware = "from sqlalchemy import DateTime\nx = mapped_column(DateTime(timezone=True))\n"
    assert _colonne_aware(aware) == [(2, "DateTime(timezone=True)")]

    naive = "from sqlalchemy import DateTime\nx = mapped_column(DateTime, default=oggi)\n"
    assert _colonne_aware(naive) == []

    esplicito_falso = "x = mapped_column(DateTime(timezone=False))\n"
    assert _colonne_aware(esplicito_falso) == []
