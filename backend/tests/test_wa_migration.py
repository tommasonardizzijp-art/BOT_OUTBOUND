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
GIA' esistente (027 -> bot_state.wa_halted*) vanno tolte a parte: il modello
corrente le ha gia', ma a "stamp 024" non devono esistere, altrimenti la 027
duplica la colonna.
"""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine

BACKEND_DIR = Path(__file__).resolve().parent.parent

# Le 9 tabelle che la 025 crea (8 wa_* + tenants). Usato sia per fabbricare lo
# schema "pre" (tutto tranne queste) sia per verificare il diff "post".
WA_NEW_TABLES = {
    "tenants",
    "wa_numbers",
    "wa_contacts",
    "wa_campaigns",
    "wa_sequence_steps",
    "wa_campaign_contacts",
    "wa_messages",
    "wa_inbound_events",
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

    # bot_state esiste gia' pre-025 (migrazione 007): la 027 (successiva)
    # le aggiunge le colonne wa_halted*, che pero' il modello SQLAlchemy
    # corrente ha gia' -- create_all le avrebbe create qui sopra insieme al
    # resto della tabella. Le togliamo per fabbricare fedelmente lo stato
    # "prima della 027".
    conn = sqlite3.connect(str(db_path))
    try:
        for col in ("wa_halted", "wa_halted_reason", "wa_halted_at", "wa_halted_by"):
            conn.execute(f"ALTER TABLE bot_state DROP COLUMN {col}")
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
    sono le 9 tabelle nuove (+ i loro indici), alembic_version, e bot_state
    (027 ci aggiunge il kill-switch wa_halted: ALTER additivo intenzionale su
    una tabella IG pre-esistente, non una regressione — la sua entry DDL
    cambia testo per via delle colonne nuove, ma non sparisce ne' cambia
    struttura di quelle vecchie)."""
    pre = _sqlite_master(migration_db)
    pre_set = set(pre)
    pre_tables = {tbl for (_typ, _name, tbl, _sql) in pre}

    _run_alembic(["stamp", "024"], migration_db)
    _run_alembic(["upgrade", "head"], migration_db)

    post = _sqlite_master(migration_db)
    post_set = set(post)
    post_tables = {tbl for (_typ, _name, tbl, _sql) in post}

    pre_excluding_bot_state = {row for row in pre_set if row[2] != "bot_state"}
    assert pre_excluding_bot_state <= post_set, "un oggetto IG preesistente e' sparito o e' cambiato (ALTER?)"
    assert any(row[2] == "bot_state" for row in post_set), "bot_state e' sparita del tutto (non solo alterata)"

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
