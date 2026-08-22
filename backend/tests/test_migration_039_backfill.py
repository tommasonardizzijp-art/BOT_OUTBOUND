"""Il backfill della 039 sui DATI, non sullo schema.

`test_wa_migration.py` verifica che le migrazioni salgano e scendano: lavora su un
DB vuoto, quindi un backfill sbagliato ci passa attraverso verde. Qui si semina la
tabella e si eseguono le VERE istruzioni SQL della migration, estratte dal file
sorgente invece di essere ricopiate: una copia diverge dall'originale al primo
ritocco e nessuno se ne accorge.

Cosa si pretende:
  - la normalizzazione SQL da' lo stesso risultato di `normalizza_username`;
  - i segnaposto dei profili chiusi NON ricevono la chiave;
  - fra doppioni vince il piu' vecchio per created_at, e gli altri perdono la
    chiave ma non la riga;
  - dopo il backfill l'indice UNIQUE si crea davvero (se restasse un doppione la
    migration esploderebbe in produzione, non qui).
"""
import re
import sqlite3
from pathlib import Path

import pytest

from app.services.inbox_browser.targa import normalizza_username

MIGRAZIONE = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "039_username_norm_global_contacts.py"


def _istruzioni_sql():
    """Le op.execute() della migration, in ordine, prese dal file vero."""
    testo = MIGRAZIONE.read_text(encoding="utf-8")
    corpo = testo[testo.index("def upgrade"):testo.index("def downgrade")]
    return re.findall(r'op\.execute\("""(.*?)"""\)', corpo, re.DOTALL)


def _db_seminato(righe):
    con = sqlite3.connect(":memory:")
    con.execute("""
        CREATE TABLE global_contacts (
            id TEXT PRIMARY KEY, username TEXT, username_norm TEXT, created_at TEXT
        )
    """)
    con.executemany(
        "INSERT INTO global_contacts (id, username, created_at) VALUES (?, ?, ?)", righe
    )
    for sql in _istruzioni_sql():
        con.execute(sql)
    return con


def _norm(con, id_):
    return con.execute(
        "SELECT username_norm FROM global_contacts WHERE id = ?", (id_,)
    ).fetchone()[0]


def test_le_istruzioni_sono_tre():
    """Backfill, esclusione segnaposto, scelta del vincitore. Se questo numero
    cambia, il test qui sotto sta misurando una migration diversa da quella che
    crede."""
    assert len(_istruzioni_sql()) == 3


@pytest.mark.parametrize("grezzo", [
    "mario.rossi", "@mario.rossi", "  MARIO.Rossi  ", "@@mario.rossi",
    "shop_123", "@ mario",
])
def test_la_normalizzazione_sql_combacia_con_quella_python(grezzo):
    """Se le due divergono la colonna non fa da ponte: il canale browser cerca con
    la forma Python e non trova la riga scritta con la forma SQL."""
    con = _db_seminato([("r1", grezzo, "2026-01-01")])
    atteso = normalizza_username(grezzo)
    ottenuto = _norm(con, "r1")
    # "@ mario" normalizza a " mario", che il filtro segnaposto azzera: giusto cosi'.
    if atteso.strip() != atteso or " " in atteso:
        assert ottenuto is None
    else:
        assert ottenuto == atteso


def test_il_segnaposto_non_prende_la_chiave():
    con = _db_seminato([
        ("r1", "utente instagram", "2026-01-01"),
        ("r2", "Instagram User", "2026-01-02"),
        ("r3", "borderline_grow", "2026-01-03"),
    ])
    assert _norm(con, "r1") is None
    assert _norm(con, "r2") is None
    assert _norm(con, "r3") == "borderline_grow"
    # Le righe restano: perdono la chiave, non l'esistenza.
    assert con.execute("SELECT COUNT(*) FROM global_contacts").fetchone()[0] == 3


def test_due_profili_chiusi_non_si_fondono():
    """Senza l'esclusione dei segnaposto uno dei due prenderebbe la chiave
    'utente instagram' e l'altro la perderebbe: due persone diverse, una sola
    identita'. Qui nessuno dei due la prende."""
    con = _db_seminato([
        ("r1", "utente instagram", "2026-01-01"),
        ("r2", "utente instagram", "2026-01-02"),
    ])
    assert _norm(con, "r1") is None
    assert _norm(con, "r2") is None


def test_fra_doppioni_vince_il_piu_vecchio_per_created_at():
    """L'id e' un UUID stringa: ordinarlo non ha significato temporale. Il vincitore
    deve essere il piu' vecchio VERO, altrimenti la riga che il cliente ha gia'
    lavorato puo' perdere la chiave a favore di una piu' recente."""
    con = _db_seminato([
        ("zzz-piu-vecchia", "mario_shop", "2026-01-01"),
        ("aaa-piu-recente", "mario_shop", "2026-06-01"),
    ])
    assert _norm(con, "zzz-piu-vecchia") == "mario_shop"
    assert _norm(con, "aaa-piu-recente") is None


def test_dopo_il_backfill_l_unique_si_crea():
    """La prova che conta: se restasse un doppione, questo indice esploderebbe in
    produzione durante la migration, non qui."""
    con = _db_seminato([
        ("r1", "mario_shop", "2026-01-01"),
        ("r2", "@Mario_Shop", "2026-02-01"),
        ("r3", "utente instagram", "2026-03-01"),
        ("r4", "utente instagram", "2026-04-01"),
        ("r5", "", "2026-05-01"),
        ("r6", None, "2026-06-01"),
    ])
    con.execute(
        "CREATE UNIQUE INDEX ux ON global_contacts (username_norm) "
        "WHERE username_norm IS NOT NULL"
    )
    chiavi = [r[0] for r in con.execute(
        "SELECT username_norm FROM global_contacts WHERE username_norm IS NOT NULL"
    )]
    assert chiavi == ["mario_shop"]
