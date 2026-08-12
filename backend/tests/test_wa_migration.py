"""QA batch 4 -- funzionali A (1-3): migrazione 025.

Ciclo su-giu-su isolato ALLA SOLA 025 (alembic stamp 024, non la catena
001-024 da zero): la migrazione 013 preesistente usa ALTER COLUMN, che su
SQLite non gira su un DB vuoto senza batch mode -- vedi qa-wa-m1-tests.md
SS A.1. E' anche lo scenario reale: la produzione e' gia' a 024.

Ogni invocazione `alembic` gira in un SUBPROCESS con DATABASE_URL esplicito
puntato a un file SQLite usa-e-getta (pytest tmp_path): MAI il DB di test
della suite (tests/conftest.py punta a data/test_bot.db) e MAI il default di
config.py (data/bot.db) -- il downgrade droppa tabelle.

Lo stato "prod gia' a 024" viene fabbricato con i modelli SQLAlchemy REALI
(create_all filtrato, escludendo le tabelle che la 025 introduce): e' piu'
fedele di DDL scritto a mano, e non serve rigiocare la catena di migrazioni.
Le colonne che una migrazione SUCCESSIVA alla 025 aggiunge a una tabella
GIA' esistente (027 -> bot_state.wa_halted*, 029 -> campaigns.enrichment_level,
030 -> instagram_accounts.daily_likes_today/date, ...) vanno tolte a parte:
il modello corrente le ha gia', ma a "stamp 024"
non devono esistere, altrimenti la migrazione che le introduce duplica la
colonna. Il drop e' condizionale (PRAGMA table_info) perche' questo fixture
gira anche su alberi dove quella colonna non e' ancora nel modello (es.
l'albero WhatsApp, dove campaigns.enrichment_level non esiste affatto).
"""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine

BACKEND_DIR = Path(__file__).resolve().parent.parent

# Le 9 tabelle che la catena 025->head CREA da zero (8 wa_* + tenants). Usato
# sia per fabbricare lo schema "pre" (tutto tranne queste) sia per verificare il
# diff "post". NON e' "solo quelle della 025": qualunque migrazione successiva
# che fa `create_table` va aggiunta qui, altrimenti create_all fabbrica la
# tabella nello schema "pre" e la migrazione muore su "table already exists".
WA_NEW_TABLES = {
    "tenants",
    "wa_numbers",
    "wa_contacts",
    "wa_campaigns",
    "wa_sequence_steps",
    "wa_campaign_contacts",
    "wa_messages",
    "wa_inbound_events",
    "wa_discovered_chats",  # 032
}

# Colonne che una migrazione SUCCESSIVA alla 025 aggiunge a una tabella GIA'
# esistente. create_all (coi modelli correnti) le crea gia' insieme al resto
# della tabella, ma a "stamp 024" (dove il fixture si posiziona) non devono
# esserci, altrimenti la migrazione che le introduce fa un ADD COLUMN su una
# colonna duplicata. Chiave = tabella, valore = colonne da droppare SE
# presenti (mai droppate a secco: vedi _seed_pre_025_schema). Le chiavi sono
# anche le tabelle Instagram che test_025_non_tocca_instagram si aspetta
# vedere alterate (non sparite) dall'upgrade: stessa fonte di verita', un
# solo posto da aggiornare quando una nuova migrazione aggiunge una colonna.
POST_024_COLUMNS = {
    "bot_state": ["wa_halted", "wa_halted_reason", "wa_halted_at", "wa_halted_by"],  # 027
    "campaigns": ["enrichment_level",                                    # 029
                  "inbox_cursor_at", "inbox_cursor_updated_at"],         # 033
    "instagram_accounts": ["daily_likes_today", "daily_likes_date"],  # 030
    "followers": ["last_message_at", "last_message_from",             # 031
                  "last_message_text", "source_channel"],
}


def _run_alembic(args: list[str], db_path: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    result = subprocess.run(
        [sys.executable, "-m", "alembic"] + args,
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} fallito (DATABASE_URL={env['DATABASE_URL']}):\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    return result


def _seed_pre_025_schema(db_path: Path) -> None:
    """Fabbrica lo stato 'produzione gia' a 024': tutte le tabelle tranne
    quelle che la 025 introduce, con i modelli SQLAlchemy reali (non DDL a
    mano) -- fedele allo schema attuale, nessuna dipendenza dalla catena
    alembic 001-024."""
    from app.database import Base
    import app.models  # noqa: F401 -- registra tutte le tabelle su Base.metadata

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        ig_tables = [t for t in Base.metadata.sorted_tables if t.name not in WA_NEW_TABLES]
        Base.metadata.create_all(engine, tables=ig_tables)
    finally:
        engine.dispose()

    # Le tabelle in POST_024_COLUMNS nascono sopra con TUTTE le colonne del
    # modello corrente, incluse quelle aggiunte da migration successive alla
    # 025 (027, 029, ...). Le togliamo qui per fabbricare fedelmente lo stato
    # "prima di quelle migration" -- ma SOLO se la colonna e' realmente
    # presente (PRAGMA table_info), mai con un ALTER secco: questo fixture
    # gira anche su alberi (es. WhatsApp/main) dove una data colonna non e'
    # ancora nel modello SQLAlchemy, quindi create_all non l'ha creata affatto
    # e un DROP COLUMN incondizionato fallirebbe con "no such column".
    conn = sqlite3.connect(str(db_path))
    try:
        for table, columns in POST_024_COLUMNS.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for col in columns:
                if col in existing:
                    conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
        conn.commit()
    finally:
        conn.close()


def _sqlite_master(db_path: Path) -> list[tuple]:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
    finally:
        conn.close()


@pytest.fixture
def migration_db(tmp_path) -> Path:
    db_path = tmp_path / "wa_m1_qa_migration.db"
    _seed_pre_025_schema(db_path)
    return db_path


# ---------------------------------------------------------------------------
# Funzionale 1 -- ciclo su-giu-su isolato alla 025
# ---------------------------------------------------------------------------

def test_ciclo_su_giu_su_isolato_alla_025(migration_db):
    """stamp 024 -> upgrade head -> downgrade 024 -> upgrade head: nessun
    errore in nessuno dei quattro passi."""
    _run_alembic(["stamp", "024"], migration_db)
    _run_alembic(["upgrade", "head"], migration_db)
    _run_alembic(["downgrade", "024"], migration_db)
    _run_alembic(["upgrade", "head"], migration_db)

    # dopo il secondo upgrade lo schema c'e' di nuovo, non e' rimasto a meta'
    conn = sqlite3.connect(str(migration_db))
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    finally:
        conn.close()
    assert WA_NEW_TABLES <= tables


# ---------------------------------------------------------------------------
# Funzionale 2 -- schema fisico presente (sqlite_master, non il modello)
# ---------------------------------------------------------------------------

def test_schema_fisico_presente_dopo_upgrade(migration_db):
    """Le 9 tabelle, i 3 UNIQUE inline e l'indice
    ix_wa_campaign_contacts_next_action sono FISICAMENTE nello schema creato
    dalla migrazione (via sqlite_master / PRAGMA index_list), non solo
    dichiarati nel modello SQLAlchemy."""
    _run_alembic(["stamp", "024"], migration_db)
    _run_alembic(["upgrade", "head"], migration_db)

    conn = sqlite3.connect(str(migration_db))
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
        ).fetchall()}
        assert WA_NEW_TABLES <= tables

        def table_ddl(table: str) -> str:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            assert row is not None, f"tabella {table} assente a schema"
            return row[0]

        def unique_autoindex_count(table: str) -> int:
            # SQLite non espone il NOME del vincolo UNIQUE dichiarato inline
            # in create_table (lo autonomina sqlite_autoindex_<tabella>_<N>):
            # il nome scelto nella migrazione sopravvive solo nel testo DDL
            # (CONSTRAINT <nome> UNIQUE (...)), non in PRAGMA index_list.
            # Qui si verifica CHE l'indice unico fisico esista (autoindex),
            # non con che nome.
            return sum(1 for row in conn.execute(f"PRAGMA index_list({table})").fetchall()
                       if row[2] == 1)

        # wa_contacts: 2 indici unici fisici attesi -- 1 per la PK (String,
        # non INTEGER: SQLite crea comunque un autoindex) + 1 per lo
        # UniqueConstraint(tenant_id, phone_hmac). Il nome del vincolo e'
        # verificato via testo DDL (CONSTRAINT ...), che e' cio' che il
        # driver Postgres di produzione legge davvero.
        assert unique_autoindex_count("wa_contacts") == 2
        assert "CONSTRAINT uq_wa_contacts_tenant_phone UNIQUE" in table_ddl("wa_contacts")

        assert unique_autoindex_count("wa_sequence_steps") == 2
        assert "CONSTRAINT uq_wa_steps_campaign_index UNIQUE" in table_ddl("wa_sequence_steps")

        assert unique_autoindex_count("wa_campaign_contacts") == 2
        assert "CONSTRAINT uq_wa_camp_contact UNIQUE" in table_ddl("wa_campaign_contacts")

        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='ix_wa_campaign_contacts_next_action' AND tbl_name='wa_campaign_contacts'"
        ).fetchone()
        assert idx is not None, "indice ix_wa_campaign_contacts_next_action assente a schema"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Funzionale 3 -- la 025 non tocca il canale Instagram
# ---------------------------------------------------------------------------

def test_025_non_tocca_instagram(migration_db):
    """Confronto sqlite_master pre/post upgrade: ogni oggetto IG pre-esistente
    resta presente con lo STESSO DDL testuale (un ALTER su SQLite riscrive la
    tabella, quindi cambierebbe/sparirebbe la entry); le uniche differenze
    sono le 9 tabelle nuove (+ i loro indici), alembic_version, e le tabelle
    in POST_024_COLUMNS (027 -> bot_state.wa_halted*, 029 ->
    campaigns.enrichment_level: ALTER additivi intenzionali su tabelle IG
    pre-esistenti, non regressioni -- la loro entry DDL cambia testo per via
    delle colonne nuove, ma non sparisce ne' cambia struttura delle altre
    tabelle vecchie). Le tabelle attese alterate sono le stesse chiavi usate
    da _seed_pre_025_schema per fabbricare lo schema "pre": se domani una
    migrazione altera una tabella IG NON registrata li', questo assert deve
    restare capace di accorgersene e fallire."""
    pre = _sqlite_master(migration_db)
    pre_set = set(pre)
    pre_tables = {tbl for (_typ, _name, tbl, _sql) in pre}

    _run_alembic(["stamp", "024"], migration_db)
    _run_alembic(["upgrade", "head"], migration_db)

    post = _sqlite_master(migration_db)
    post_set = set(post)
    post_tables = {tbl for (_typ, _name, tbl, _sql) in post}

    altered_tables = set(POST_024_COLUMNS.keys())
    pre_excluding_altered = {row for row in pre_set if row[2] not in altered_tables}
    assert pre_excluding_altered <= post_set, "un oggetto IG preesistente e' sparito o e' cambiato (ALTER?)"
    for table in altered_tables:
        assert any(row[2] == table for row in post_set), f"{table} e' sparita del tutto (non solo alterata)"

    new_tables = post_tables - pre_tables
    assert new_tables == WA_NEW_TABLES | {"alembic_version"}


# ---------------------------------------------------------------------------
# Funzionale 4 -- 028: warmup_advanced_date raggiungibile via upgrade pulito
# ---------------------------------------------------------------------------

def _wa_numbers_columns(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(wa_numbers)").fetchall()}
    finally:
        conn.close()


def test_028_warmup_advanced_date_raggiungibile_da_upgrade_pulito(migration_db):
    """stamp 024 (prod reale) -> upgrade head: la colonna nuova (M5,
    avanzamento automatico warmup_day) e' fisicamente su wa_numbers."""
    _run_alembic(["stamp", "024"], migration_db)
    _run_alembic(["upgrade", "head"], migration_db)

    assert "warmup_advanced_date" in _wa_numbers_columns(migration_db)


def test_028_ciclo_su_giu_su_isolato(migration_db):
    """downgrade 027 (via batch_alter_table) -> upgrade head: nessun errore,
    e la colonna torna ad esserci dopo il secondo upgrade."""
    _run_alembic(["stamp", "024"], migration_db)
    _run_alembic(["upgrade", "head"], migration_db)
    assert "warmup_advanced_date" in _wa_numbers_columns(migration_db)

    _run_alembic(["downgrade", "027"], migration_db)
    assert "warmup_advanced_date" not in _wa_numbers_columns(migration_db)

    _run_alembic(["upgrade", "head"], migration_db)
    assert "warmup_advanced_date" in _wa_numbers_columns(migration_db)


# ---------------------------------------------------------------------------
# Funzionale 5 -- 030: tetto giornaliero like, raggiungibile e ciclo su-giu-su
# ---------------------------------------------------------------------------

def _instagram_accounts_columns(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(instagram_accounts)").fetchall()}
    finally:
        conn.close()


def test_030_daily_likes_raggiungibile_da_upgrade_pulito(migration_db):
    """stamp 024 (prod reale) -> upgrade head: le due colonne nuove (B.3,
    tetto giornaliero like persistito) sono fisicamente su instagram_accounts.
    E' il caso che duplicava la colonna prima del fix di POST_024_COLUMNS
    (il fixture, costruito sui modelli correnti, le creava gia' a "stamp 024",
    e l'ADD COLUMN della 030 falliva con "duplicate column name")."""
    _run_alembic(["stamp", "024"], migration_db)
    _run_alembic(["upgrade", "head"], migration_db)

    cols = _instagram_accounts_columns(migration_db)
    assert "daily_likes_today" in cols
    assert "daily_likes_date" in cols


def test_030_ciclo_su_giu_su_isolato(migration_db):
    """downgrade 029 (via batch_alter_table) -> upgrade head: nessun errore,
    entrambe le colonne tornano ad esserci dopo il secondo upgrade. Profilo
    diverso dalla 028 (colonna singola nullable): qui daily_likes_today e'
    NOT NULL con server_default, il batch rebuild di SQLite deve reggerlo."""
    _run_alembic(["stamp", "024"], migration_db)
    _run_alembic(["upgrade", "head"], migration_db)
    cols = _instagram_accounts_columns(migration_db)
    assert "daily_likes_today" in cols
    assert "daily_likes_date" in cols

    _run_alembic(["downgrade", "029"], migration_db)
    cols = _instagram_accounts_columns(migration_db)
    assert "daily_likes_today" not in cols
    assert "daily_likes_date" not in cols

    _run_alembic(["upgrade", "head"], migration_db)
    cols = _instagram_accounts_columns(migration_db)
    assert "daily_likes_today" in cols
    assert "daily_likes_date" in cols
