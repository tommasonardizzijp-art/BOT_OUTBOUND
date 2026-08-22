"""Ponte fra le due rappresentazioni della stessa persona in anagrafica.

Il canale API identifica un contatto per pk reale (`ig_user_id` positivo). Il
canale browser non lo conosce e usa la targa provvisoria (`inbox_browser/targa.py`
-- SHA-256 negato dello username normalizzato). Finche' l'unica chiave era
`ig_user_id`, la stessa persona vista dai due canali produceva DUE righe distinte
in `global_contacts`: misurato su prod il 21/08, 32 doppioni su 34 righe.

`username_norm` e' quel ponte: la stessa colonna, calcolata con la STESSA
normalizzazione usata dal canale browser per la targa (`normalizza_username`),
scritta sia sul pk reale sia sulla targa provvisoria. L'indice UNIQUE parziale fa
convergere le due rappresentazioni su una riga sola -- e' cio' che impedisce ai 32
doppioni di tornare.

Backfill: la normalizzazione SQL qui sotto e' stata verificata di proposito contro
`normalizza_username` (eseguendo le due versioni, non a intuito -- vedi handoff
Task 4). La formula del piano originale, `lower(trim(ltrim(trim(x), '@')))`,
diverge da `normalizza_username` quando resta uno spazio subito dopo la chiocciola
tolta (es. "@ mario" -> Python " mario", quella formula "mario": il trim esterno
ripulisce uno spazio che Python lascia). La formula qui sotto,
`lower(ltrim(trim(x), '@'))` (senza il trim esterno), da' lo STESSO risultato di
`normalizza_username` su tutti i casi verificati (chiocciole multiple, spazi dopo
la chiocciola, maiuscole, stringa vuota/solo spazi, None). Resta un'unica
divergenza nota, minore: SQLite `trim()` senza argomenti toglie solo lo spazio
ASCII, non tab/newline, mentre Python `str.strip()` toglie ogni whitespace --
irrilevante in pratica perche' tab/newline non sono caratteri validi in uno
username Instagram e non supererebbero comunque `handle_valido()`. Riguarda solo
questo backfill una tantum: il percorso live (Task 5, `upsert_lead`) chiama
`normalizza_username` in Python, non questa formula SQL.

Scelta del vincitore fra doppioni pre-esistenti: il modello ha `created_at`
(`DateTime`, `nullable=False`), quindi si sceglie il piu' vecchio VERO per
timestamp invece del piu' vecchio lessicografico per `id` (UUID stringa, ordine
senza significato temporale). Pareggio di timestamp spareggiato per `id` per
restare comunque deterministico.

Revision ID: 039
Revises: 038
"""
from alembic import op
import sqlalchemy as sa

revision = "039"
down_revision = "038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("global_contacts", sa.Column("username_norm", sa.Text(), nullable=True))

    # Backfill dalla colonna username gia' presente. Normalizzazione allineata a
    # normalizza_username (vedi docstring in testa al file per la verifica).
    op.execute("""
        UPDATE global_contacts
        SET username_norm = lower(ltrim(trim(username), '@'))
        WHERE username IS NOT NULL AND trim(username) <> ''
    """)

    # I SEGNAPOSTO storici non prendono la chiave. Instagram mostra a tutti i
    # profili chiusi/disattivati lo stesso testo ("Utente di Instagram", e
    # l'equivalente in ogni lingua), e quel testo e' finito nel campo username di
    # righe gia' in tabella. Il backfill qui sopra normalizza qualunque username
    # non vuoto, quindi senza questo passaggio uno di quei segnaposto diventerebbe
    # la chiave d'identita' di una riga: da domani un handle che non appartiene a
    # nessuno. Non e' un rischio teorico, il log del 22/08 lo mostra in produzione
    # (`[InboxLista] @utente instagram esiste gia' con una targa REALE diversa`).
    #
    # Il filtro e' piu' STRETTO nel codice vivo (`handle_valido`, che accetta solo
    # [a-z0-9._] fino a 30 caratteri) e piu' LARGO qui: spazio e lunghezza si
    # esprimono in SQL identico su SQLite e PostgreSQL, una classe di caratteri no.
    # Basta allo scopo — ogni segnaposto conosciuto contiene uno spazio — e cio'
    # che sfugge resta comunque escluso dal percorso vivo, che passa da
    # handle_valido. Le righe restano intatte: perdono solo il ruolo di chiave.
    op.execute("""
        UPDATE global_contacts SET username_norm = NULL
        WHERE username_norm IS NOT NULL
          AND (username_norm LIKE '% %' OR length(username_norm) > 30)
    """)

    # I doppioni pre-esistenti impedirebbero l'indice UNIQUE: azzera username_norm
    # su tutti tranne il piu' vecchio (per created_at, spareggio per id) di ogni
    # gruppo. Non cancella righe e non tocca `username`: il dato resta leggibile,
    # perde solo il ruolo di chiave.
    op.execute("""
        UPDATE global_contacts SET username_norm = NULL
        WHERE username_norm IS NOT NULL AND id NOT IN (
            SELECT (
                SELECT g2.id FROM global_contacts g2
                WHERE g2.username_norm = g1.username_norm
                ORDER BY g2.created_at ASC, g2.id ASC
                LIMIT 1
            )
            FROM global_contacts g1
            WHERE g1.username_norm IS NOT NULL
            GROUP BY g1.username_norm
        )
    """)

    op.create_index(
        "ux_global_contacts_username_norm", "global_contacts", ["username_norm"],
        unique=True, sqlite_where=sa.text("username_norm IS NOT NULL"),
        postgresql_where=sa.text("username_norm IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_global_contacts_username_norm", table_name="global_contacts")
    op.drop_column("global_contacts", "username_norm")
