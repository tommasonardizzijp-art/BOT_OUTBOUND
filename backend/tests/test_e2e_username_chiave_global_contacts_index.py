"""E2E — `username_norm` (migration 039) su uno schema DAVVERO migrato, non su
Base.metadata.create_all: quel metodo crea le colonne del modello ORM corrente ma
NON l'indice UNIQUE parziale, che esiste solo nel DDL della migration
(`op.create_index(..., sqlite_where=...)`, mai specchiato in `__table_args__` sul
modello — verificato per grep). Un test costruito con create_all misurerebbe zero
sulla domanda "l'indice si comporta come previsto", perche' l'indice non ci
sarebbe affatto.

Si fabbrica quindi lo stato "produzione gia' a 038" con Base.metadata.create_all
(la colonna username_norm tolta a mano), si fa `alembic stamp 038` e poi
`alembic upgrade head` in un SUBPROCESS — stesso pattern verificato di
tests/test_wa_migration.py, isolato alla sola 039 (la catena 001-038 non gira su
SQLite da zero: la 013 usa ALTER COLUMN, che sqlite3 non supporta senza batch
mode — misurato: `alembic upgrade head` da 001 esplode su
"ALTER TABLE campaigns ALTER COLUMN target_username DROP NOT NULL").
"""
import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _run_alembic(args: list[str], db_path: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    result = subprocess.run(
        [sys.executable, "-m", "alembic"] + args,
        cwd=str(BACKEND_DIR), env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} fallito:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    return result


def _seed_pre_039_schema(db_path: Path) -> None:
    from app.database import Base
    import app.models  # noqa: F401 -- registra tutte le tabelle

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()

    conn = sqlite3.connect(str(db_path))
    try:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(global_contacts)").fetchall()}
        if "username_norm" in existing:
            conn.execute("ALTER TABLE global_contacts DROP COLUMN username_norm")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def migrated_db(tmp_path) -> Path:
    db_path = tmp_path / "e2e_username_norm.db"
    _seed_pre_039_schema(db_path)
    _run_alembic(["stamp", "038"], db_path)
    _run_alembic(["upgrade", "head"], db_path)
    return db_path


def _indice_unico_presente(db_path: Path) -> bool:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='ux_global_contacts_username_norm'"
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def test_lo_schema_migrato_ha_davvero_lindice_fisico(migrated_db):
    """Precondizione degli altri test: se questo fallisse, gli assert sotto
    misurerebbero il comportamento di SQLite senza vincolo, non quello vero."""
    assert _indice_unico_presente(migrated_db)


# ── Comportamento dell'indice UNIQUE parziale ────────────────────────────────

def _insert(db_path: Path, *, ig_user_id: int, username_norm: str | None):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO global_contacts "
            "(id, ig_user_id, username, username_norm, scrape_sources, "
            " contacted_by_campaign_ids, contact_history, created_at) "
            "VALUES (?, ?, ?, ?, '[]', '[]', '[]', datetime('now'))",
            (str(uuid.uuid4()), ig_user_id, username_norm or "x", username_norm),
        )
        conn.commit()
    finally:
        conn.close()


def test_due_righe_con_username_norm_null_coesistono(migrated_db):
    _insert(migrated_db, ig_user_id=1001, username_norm=None)
    _insert(migrated_db, ig_user_id=1002, username_norm=None)  # non deve sollevare
    conn = sqlite3.connect(str(migrated_db))
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM global_contacts WHERE username_norm IS NULL"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 2


def test_due_righe_con_lo_stesso_username_norm_collidono(migrated_db):
    _insert(migrated_db, ig_user_id=2001, username_norm="stesso_handle")
    with pytest.raises(sqlite3.IntegrityError):
        _insert(migrated_db, ig_user_id=2002, username_norm="stesso_handle")


# ── Il codice vivo (upsert_lead) contro lo schema migrato ───────────────────

import asyncio  # noqa: E402

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.models.global_contact import GlobalContact  # noqa: E402
from app.services.global_contact_service import upsert_lead  # noqa: E402
from app.utils.contact_extract import ContactData  # noqa: E402


class _CampaignFinta:
    id = "campagna-e2e-username-norm"
    name = "e2e username_norm"


class _AccountFinto:
    id = "account-e2e"
    username = "scraper_account"


def _session_factory(db_path: Path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path.as_posix()}", connect_args={"check_same_thread": False},
    )
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def test_upsert_lead_su_schema_migrato_non_si_rompe(migrated_db):
    """L'insert e il merge del codice ATTUALE (che non tocca username_norm)
    devono passare indenni sulla colonna e sull'indice nuovi: nessuna
    eccezione, nessun IntegrityError inatteso."""
    engine, sf = _session_factory(migrated_db)

    async def _go():
        async with sf() as db:
            await upsert_lead(
                db, ig_user_id=3001, username="alfa", full_name="Alfa",
                biography="", contacts=ContactData(), campaign=_CampaignFinta(), account=_AccountFinto(),
            )
            await upsert_lead(
                db, ig_user_id=3002, username="beta", full_name="Beta",
                biography="", contacts=ContactData(), campaign=_CampaignFinta(), account=_AccountFinto(),
            )
            # Merge sulla riga 3001 gia' esistente: stesso percorso, non insert.
            await upsert_lead(
                db, ig_user_id=3001, username="alfa", full_name="Alfa Aggiornato",
                biography="bio nuova", contacts=ContactData(), campaign=_CampaignFinta(), account=_AccountFinto(),
            )
            righe = (await db.execute(
                select(GlobalContact).where(GlobalContact.ig_user_id.in_([3001, 3002]))
            )).scalars().all()
            return {r.ig_user_id: r for r in righe}

    try:
        per_pk = asyncio.run(_go())
    finally:
        asyncio.run(engine.dispose())

    assert set(per_pk) == {3001, 3002}, "l'insert/merge non ha scritto le righe attese"
    assert per_pk[3001].full_name == "Alfa Aggiornato", "il merge non e' avvenuto"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Lavoro mancante, non difetto di cio' che e' stato consegnato: scrivere "
        "username_norm negli upsert e' lo Step 4 della Task 5 del piano "
        "(2026-08-22-username-chiave-di-prima-classe.md), non ancora iniziata. "
        "La migration 039 backfilla le 13.342 righe gia' in tabella, ma il codice "
        "vivo non valorizza mai la colonna: ogni contatto nuovo nasce con NULL, e "
        "il ponte fra pk reale e targa provvisoria smette di formarsi appena i dati "
        "nuovi superano i vecchi. strict=True di proposito: quando la Task 5 "
        "atterra, questo test torna verde DA SOLO e xfail_strict lo segnala come "
        "XPASS invece di lasciarlo passare in silenzio."
    ),
)
def test_DIFETTO_upsert_lead_non_valorizza_mai_username_norm(migrated_db):
    """Trovato collaudando lo scenario 4: il codice vivo non scrive MAI
    `username_norm` (verificato per grep: l'unico riferimento fuori dal modello
    e dalla migration e' un commento non correlato in scrape_inbox.py). La
    migration 039 lo popola SOLO in backfill, una tantum, sulle righe gia' in
    tabella al momento della migrazione — non su quelle create dopo.

    Effetto pratico: l'indice UNIQUE parziale esiste ed e' corretto (vedi i due
    test sopra), ma per ogni contatto scritto DA QUESTO CANTIERE IN POI la sua
    colonna resta NULL, quindi l'indice non lo protegge mai — il "ponte" fra pk
    reale e targa provvisoria descritto nella docstring della migration
    (global_contact_service.py:1-13) smette di formarsi non appena i dati
    vecchi vengono superati da quelli nuovi.

    Questo test verifica il comportamento CORRETTO atteso (username_norm
    valorizzato con normalizza_username(username) dopo insert e dopo merge) ed
    e' quindi RED oggi: non e' un bug nell'harness, e' il difetto stesso reso
    eseguibile. Non lo marchiamo xfail: il rosso E' il risultato del collaudo."""
    from app.services.inbox_browser.targa import normalizza_username

    engine, sf = _session_factory(migrated_db)

    async def _go():
        async with sf() as db:
            await upsert_lead(
                db, ig_user_id=4001, username="Gamma_Shop", full_name="Gamma",
                biography="", contacts=ContactData(), campaign=_CampaignFinta(), account=_AccountFinto(),
            )
            riga = (await db.execute(
                select(GlobalContact).where(GlobalContact.ig_user_id == 4001)
            )).scalar_one()
            return riga.username_norm

    try:
        ottenuto = asyncio.run(_go())
    finally:
        asyncio.run(engine.dispose())

    assert ottenuto == normalizza_username("Gamma_Shop"), (
        f"DIFETTO confermato: username_norm e' {ottenuto!r} invece di essere "
        "valorizzato da upsert_lead — il ponte pk/targa non si forma sui contatti nuovi."
    )
