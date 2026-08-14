# Lancio dell'auto-discover WhatsApp dalla UI — piano di implementazione

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **REQUIRED SUB-SKILL aggiuntiva (standard di questo progetto):** `sviluppo-modulo` — implementer + reviewer dedicato per ogni task, QA agent dopo ogni task, e alla fine il protocollo di chiusura (§ Chiusura del modulo).

**Goal:** dare all'operatore un bottone "Scansiona contatti" su ogni numero WhatsApp, che lancia l'auto-discover della Fase A come job asincrono e ne mostra esito e storico, senza mai far girare due browser insieme.

**Architecture:** un endpoint `POST /api/wa/numbers/{id}/discover` applica sei guardie fail-closed, apre una riga in `wa_discover_runs` e accoda un job ARQ con `_job_id` legato al `run_id`; il worker chiama `esegui_discover_run`, già esistente e collaudato, e chiude la riga con i contatori. La UI polla `GET /api/wa/numbers/{id}/discover` con lo stesso schema del bottone "Attività organica" di Instagram. Il motore si tocca in due punti soltanto: il salto delle chat già note (senza cui una riscansione costa ore) e il gate di sincronizzazione, che oggi non funziona.

**Tech Stack:** FastAPI · SQLAlchemy 2.0 async · Alembic · ARQ su Redis · Patchright/Playwright · pytest-asyncio · Next.js App Router · SWR · sonner

**Spec:** `docs/superpowers/specs/2026-08-14-wa-discover-lancio-ui-design.md`

## Global Constraints

- **Worktree:** `D:\BOT OUTBOUND\.worktrees\wa-discover-lancio`, branch `feat/wa-discover-lancio`, da `origin/main` @ `94de751`. Mai push diretto su `main`: PR.
- **Migrazione:** la prossima revision libera è `035`, `down_revision = "034"`. Nome file `035_wa_discover_runs.py`.
- **Tipi modello:** in `backend/app/models/wa.py` **non esiste `String36`**: si usa `String(36)`. `Base` si importa da `app.database`. Timestamp: `DateTime(timezone=True)` con `default=datetime.utcnow` lato Python, nessun `server_default`.
- **Stile endpoint:** sessione con `db=Depends(get_db)` **senza annotazione di tipo**; errori con `raise HTTPException(404, "messaggio")` posizionale; nessun `response_model`, ritorno `-> dict` costruito a mano.
- **Router già montato:** `wa_numbers.router` e `wa_discover.router` sono già in `backend/app/main.py:139-142` con `dependencies=_protected`. **Non modificare `main.py`.**
- **PII (vincolo P12):** nessun numero di telefono in chiaro in log, risposte API o colonne testuali. I numeri si mostrano solo via `mask_phone(decrypt(...))`.
- **Test backend:** `pytest` da `backend/`, con `./venv/Scripts/python.exe -m pytest`. Il `conftest.py` è **congelato** dopo PR-0: le factory nuove vanno in `backend/tests/factories_wa.py`. Una sola suite pytest alla volta (lock file per slot).
- **Dipendenze:** qualunque import nuovo va dichiarato in `backend/requirements.txt`. Una dipendenza presente solo nel venv locale rompe la **collection** in CI, non un singolo test.
- **Frontend:** nessuna infrastruttura di test esiste (niente vitest/jest/playwright, nessuno script `test`). I gate automatici sono `npm run lint` e il type-check di `next build`. Non introdurre un runner in questo cantiere.
- **Frontend, avvio:** il frontend gira dalla **root del repo**, non dal worktree — Turbopack rifiuta la junction `node_modules`.

---

## Struttura dei file

**Backend — creati**

| File | Responsabilità |
|---|---|
| `backend/alembic/versions/035_wa_discover_runs.py` | Crea la tabella e i suoi due indici |
| `backend/app/services/wa_discover_runs.py` | Ciclo di vita di una run: apri, chiudi, leggi l'ultima, leggi lo storico |
| `backend/app/services/wa_discover_gate.py` | Le sei guardie pre-lancio, funzione unica che ritorna un motivo o `None` |
| `backend/app/workers/wa_discover_worker.py` | Job ARQ `wa_discover_task` + enqueuer + costruzione del job id |

**Backend — modificati**

| File | Modifica |
|---|---|
| `backend/app/models/wa.py` | Aggiunge `WaDiscoverRun` in fondo |
| `backend/app/services/wa_profile_lock.py` | Aggiunge `profilo_occupato_da()` |
| `backend/app/services/wa_discover_run.py` | Salto delle chat note + contatore `saltate_gia_note` |
| `backend/app/services/wa_discover/sincronizzazione.py` | Gate tri-stato `letta`/`assente`/`ignota` |
| `backend/app/workers/task_queue.py` | Registra `wa_discover_task` |
| `backend/app/api/wa_numbers.py` | `POST` e `GET /{id}/discover` |
| `backend/app/config.py` | Tre impostazioni nuove |
| `backend/requirements.txt` | `psutil` |
| `backend/tests/factories_wa.py` | `make_discover_run` |

**Frontend — modificati**

| File | Modifica |
|---|---|
| `frontend/lib/waApi.ts` | Tipi `WaDiscoverRun`/`WaDiscoverStato` + `numeri.discover` e `numeri.discoverStato` |
| `frontend/app/wa/numeri/page.tsx` | Colonna "Ultimo scan" + `ScansionaContattiButton` |
| `frontend/app/wa/scoperti/page.tsx` | Testata con esito, bottone "Riscansiona", storico |

---

## Task 1: Tabella e modello delle run

**Files:**
- Create: `backend/alembic/versions/035_wa_discover_runs.py`
- Modify: `backend/app/models/wa.py` (in fondo, dopo `WaDiscoveredChat` che finisce alla riga 444)
- Modify: `backend/tests/factories_wa.py`
- Test: `backend/tests/test_wa_discover_runs_modello.py`

**Interfaces:**
- Produces: `WaDiscoverRun` (modello), `make_discover_run(db, tenant, number, **kw) -> WaDiscoverRun`

- [ ] **Step 1: Scrivi il test che fallisce**

Crea `backend/tests/test_wa_discover_runs_modello.py`:

```python
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.wa import WaDiscoverRun
from tests.factories_wa import make_discover_run, make_number, make_tenant


@pytest.mark.asyncio
async def test_run_nasce_running_con_i_contatori_a_zero(db_session):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    run = await make_discover_run(db_session, tenant, number)
    await db_session.commit()

    letta = await db_session.scalar(select(WaDiscoverRun).where(WaDiscoverRun.id == run.id))
    assert letta.stato == "running"
    assert letta.avviato_da == "manuale"
    assert (letta.salvate, letta.aggiornate, letta.saltate_gia_note,
            letta.non_verificate) == (0, 0, 0, 0)
    assert letta.finished_at is None
    assert letta.sync_stato == "ignota"


@pytest.mark.asyncio
async def test_due_run_running_sullo_stesso_numero_sono_impossibili(db_session):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await make_discover_run(db_session, tenant, number)
    await db_session.commit()

    await make_discover_run(db_session, tenant, number)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_due_run_chiuse_sullo_stesso_numero_convivono(db_session):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await make_discover_run(db_session, tenant, number, stato="done")
    await make_discover_run(db_session, tenant, number, stato="done")
    await db_session.commit()

    righe = (await db_session.execute(
        select(WaDiscoverRun).where(WaDiscoverRun.number_id == number.id))).scalars().all()
    assert len(righe) == 2
```

- [ ] **Step 2: Esegui il test e verifica che fallisca**

Run: `cd backend && ./venv/Scripts/python.exe -m pytest tests/test_wa_discover_runs_modello.py -v`
Expected: FAIL con `ImportError: cannot import name 'WaDiscoverRun'`

- [ ] **Step 3: Aggiungi il modello**

In fondo a `backend/app/models/wa.py`:

```python
class WaDiscoverRun(Base):
    """Una scansione auto-discover: quando, chi l'ha chiesta, cosa ha raccolto.

    Esiste per rispondere a "perche' stavolta ne ha trovati 12" senza aprire i
    log. Col discover periodico (cantiere 2) diventa l'unica traccia: li'
    nessuno guarda lo schermo mentre gira.

    L'indice unico PARZIALE su (number_id) WHERE stato='running' e' la guardia
    "una scansione alla volta per numero" scritta nel DB e non solo nel codice:
    due click ravvicinati sul bottone non possono aprire due run.
    """
    __tablename__ = "wa_discover_runs"
    __table_args__ = (
        Index("ix_wa_discover_runs_number_started", "number_id", "started_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"),
                                           nullable=False)
    number_id: Mapped[str] = mapped_column(String(36), ForeignKey("wa_numbers.id"),
                                           nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    stato: Mapped[str] = mapped_column(String(20), default="running", nullable=False)
    avviato_da: Mapped[str] = mapped_column(String(20), default="manuale", nullable=False)

    salvate: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    aggiornate: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    saltate_gia_note: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    non_verificate: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    dichiarato: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Percentuale 0-100 della raccolta sul dichiarato. Salvata invece che
    # ricalcolata: il conto cambia (l'incrementale ha aggiunto i salti) e una
    # run vecchia deve restare leggibile con la formula del suo tempo.
    copertura: Mapped[int | None] = mapped_column(Integer, nullable=True)
    motivo: Mapped[str] = mapped_column(String(30), default="in_corso", nullable=False)
    sync_letta: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sync_stato: Mapped[str] = mapped_column(String(10), default="ignota", nullable=False)
    errore: Mapped[str | None] = mapped_column(Text, nullable=True)
```

- [ ] **Step 4: Scrivi la migrazione**

Crea `backend/alembic/versions/035_wa_discover_runs.py`:

```python
"""wa_discover_runs: storico delle scansioni auto-discover

Revision ID: 035
Revises: 034
Create Date: 2026-08-14

Fino a oggi una scansione non lasciava traccia: l'unico modo di sapere com'era
andata era leggere lo stdout dello script che l'aveva lanciata. Con il lancio
dalla UI (e, dopo, col discover periodico dentro il reply-watcher) quella
traccia diventa l'unico posto dove guardare.

L'indice unico e' PARZIALE su stato='running', non una UniqueConstraint piena
su number_id: le run chiuse devono potersi accumulare (sono lo storico), una
sola per volta puo' essere aperta. Stessa forma della 034 su wa_messages.
"""
import sqlalchemy as sa
from alembic import op

revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None

_INDICE_UNICO = "uq_wa_discover_runs_una_running_per_numero"
_INDICE_STORICO = "ix_wa_discover_runs_number_started"


def upgrade() -> None:
    op.create_table(
        "wa_discover_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("number_id", sa.String(36), sa.ForeignKey("wa_numbers.id"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stato", sa.String(20), nullable=False),
        sa.Column("avviato_da", sa.String(20), nullable=False),
        sa.Column("salvate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("aggiornate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("saltate_gia_note", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("non_verificate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dichiarato", sa.Integer(), nullable=True),
        sa.Column("copertura", sa.Integer(), nullable=True),
        sa.Column("motivo", sa.String(30), nullable=False),
        sa.Column("sync_letta", sa.Integer(), nullable=True),
        sa.Column("sync_stato", sa.String(10), nullable=False),
        sa.Column("errore", sa.Text(), nullable=True),
    )
    op.create_index(_INDICE_STORICO, "wa_discover_runs", ["number_id", "started_at"])
    op.create_index(
        _INDICE_UNICO,
        "wa_discover_runs",
        ["number_id"],
        unique=True,
        postgresql_where=sa.text("stato = 'running'"),
        sqlite_where=sa.text("stato = 'running'"),
    )


def downgrade() -> None:
    op.drop_index(_INDICE_UNICO, table_name="wa_discover_runs")
    op.drop_index(_INDICE_STORICO, table_name="wa_discover_runs")
    op.drop_table("wa_discover_runs")
```

L'indice unico parziale **non** sta in `__table_args__` del modello: `create_all` nei test lo creerebbe senza la clausola `WHERE` in alcuni dialetti. Il test `test_due_run_running_sullo_stesso_numero_sono_impossibili` gira su SQLite, quindi il modello deve dichiararlo. Aggiungi quindi in `__table_args__`, accanto all'`Index` già presente:

```python
        Index("uq_wa_discover_runs_una_running_per_numero", "number_id",
              unique=True,
              sqlite_where=text("stato = 'running'"),
              postgresql_where=text("stato = 'running'")),
```

`text` è già importato in cima a `models/wa.py`.

- [ ] **Step 5: Aggiungi la factory**

In fondo a `backend/tests/factories_wa.py`:

```python
async def make_discover_run(db, tenant, number, *, stato: str = "running",
                            avviato_da: str = "manuale", salvate: int = 0,
                            aggiornate: int = 0, saltate_gia_note: int = 0,
                            non_verificate: int = 0, dichiarato: int | None = None,
                            copertura: int | None = None, motivo: str = "in_corso",
                            sync_stato: str = "ignota") -> WaDiscoverRun:
    run = WaDiscoverRun(
        id=str(uuid.uuid4()), tenant_id=tenant.id, number_id=number.id,
        stato=stato, avviato_da=avviato_da, salvate=salvate, aggiornate=aggiornate,
        saltate_gia_note=saltate_gia_note, non_verificate=non_verificate,
        dichiarato=dichiarato, copertura=copertura, motivo=motivo,
        sync_stato=sync_stato,
    )
    db.add(run)
    await db.flush()
    return run
```

Aggiungi `WaDiscoverRun` all'import di `app.models.wa` in cima al file.

- [ ] **Step 6: Esegui i test e verifica che passino**

Run: `cd backend && ./venv/Scripts/python.exe -m pytest tests/test_wa_discover_runs_modello.py -v`
Expected: 3 passed

- [ ] **Step 7: Commit**

```bash
git add backend/alembic/versions/035_wa_discover_runs.py backend/app/models/wa.py backend/tests/factories_wa.py backend/tests/test_wa_discover_runs_modello.py
git commit -m "feat(wa): tabella wa_discover_runs per lo storico delle scansioni"
```

---

## Task 2: Ciclo di vita della run

**Files:**
- Create: `backend/app/services/wa_discover_runs.py`
- Test: `backend/tests/test_wa_discover_runs_servizio.py`

**Interfaces:**
- Consumes: `WaDiscoverRun` (Task 1)
- Produces:
  - `async def apri_run(db, *, tenant_id: str, number_id: str, avviato_da: str = "manuale") -> WaDiscoverRun`
  - `async def chiudi_run(db, run_id: str, esito: dict, *, errore: str | None = None) -> None`
  - `async def run_attiva(db, number_id: str) -> WaDiscoverRun | None`
  - `async def ultima_run(db, number_id: str) -> WaDiscoverRun | None`
  - `async def storico(db, number_id: str, *, limit: int = 10) -> list[WaDiscoverRun]`
  - `def calcola_copertura(esito: dict) -> int | None`

- [ ] **Step 1: Scrivi il test che fallisce**

Crea `backend/tests/test_wa_discover_runs_servizio.py`:

```python
import pytest

from app.services import wa_discover_runs
from tests.factories_wa import make_discover_run, make_number, make_tenant


def test_copertura_include_i_salti():
    # Senza i salti una riscansione riuscita sembrerebbe una raccolta al 2%.
    esito = {"salvate": 3, "aggiornate": 2, "saltate_gia_note": 90,
             "non_verificate": 0, "dichiarato": 100}
    assert wa_discover_runs.calcola_copertura(esito) == 95


def test_copertura_none_se_il_dichiarato_manca():
    esito = {"salvate": 10, "aggiornate": 0, "saltate_gia_note": 0,
             "non_verificate": 0, "dichiarato": None}
    assert wa_discover_runs.calcola_copertura(esito) is None


def test_copertura_none_se_il_dichiarato_e_zero():
    esito = {"salvate": 0, "aggiornate": 0, "saltate_gia_note": 0,
             "non_verificate": 0, "dichiarato": 0}
    assert wa_discover_runs.calcola_copertura(esito) is None


def test_copertura_non_supera_cento():
    # Il dichiarato di WhatsApp non e' affidabile al singolo: una raccolta
    # superiore non deve produrre "137%" in UI.
    esito = {"salvate": 137, "aggiornate": 0, "saltate_gia_note": 0,
             "non_verificate": 0, "dichiarato": 100}
    assert wa_discover_runs.calcola_copertura(esito) == 100


@pytest.mark.asyncio
async def test_apri_run_la_rende_visibile_come_attiva(db_session):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)

    run = await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id,
                                          number_id=number.id)
    await db_session.commit()

    attiva = await wa_discover_runs.run_attiva(db_session, number.id)
    assert attiva is not None and attiva.id == run.id


@pytest.mark.asyncio
async def test_chiudi_run_scrive_contatori_stato_e_copertura(db_session):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    run = await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id,
                                          number_id=number.id)
    await db_session.commit()

    await wa_discover_runs.chiudi_run(db_session, run.id, {
        "salvate": 60, "aggiornate": 5, "saltate_gia_note": 20,
        "non_verificate": 2, "dichiarato": 100, "motivo": "completato",
        "sync_letta": None, "sync_stato": "assente",
    })
    await db_session.commit()

    assert await wa_discover_runs.run_attiva(db_session, number.id) is None
    chiusa = await wa_discover_runs.ultima_run(db_session, number.id)
    assert chiusa.stato == "done"
    assert chiusa.finished_at is not None
    assert (chiusa.salvate, chiusa.aggiornate, chiusa.saltate_gia_note) == (60, 5, 20)
    assert chiusa.copertura == 85
    assert chiusa.motivo == "completato"
    assert chiusa.sync_stato == "assente"


@pytest.mark.asyncio
async def test_chiudi_run_con_errore_va_in_failed(db_session):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    run = await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id,
                                          number_id=number.id)
    await db_session.commit()

    await wa_discover_runs.chiudi_run(db_session, run.id, {},
                                      errore="RuntimeError: browser sparito")
    await db_session.commit()

    chiusa = await wa_discover_runs.ultima_run(db_session, number.id)
    assert chiusa.stato == "failed"
    assert chiusa.motivo == "errore_imprevisto"
    assert "browser sparito" in chiusa.errore


@pytest.mark.asyncio
async def test_chiudi_run_gia_chiusa_non_la_riapre(db_session):
    # Il worker puo' chiamare chiudi_run due volte (esito + finally di guardia):
    # la seconda non deve sovrascrivere finished_at ne' i contatori.
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    run = await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id,
                                          number_id=number.id)
    await db_session.commit()
    await wa_discover_runs.chiudi_run(db_session, run.id,
                                      {"salvate": 7, "motivo": "completato"})
    await db_session.commit()
    primo_finished = (await wa_discover_runs.ultima_run(db_session, number.id)).finished_at

    await wa_discover_runs.chiudi_run(db_session, run.id, {}, errore="tardiva")
    await db_session.commit()

    chiusa = await wa_discover_runs.ultima_run(db_session, number.id)
    assert chiusa.stato == "done"
    assert chiusa.salvate == 7
    assert chiusa.finished_at == primo_finished


@pytest.mark.asyncio
async def test_storico_torna_le_run_dalla_piu_recente(db_session):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    vecchia = await make_discover_run(db_session, tenant, number, stato="done",
                                      motivo="completato")
    recente = await make_discover_run(db_session, tenant, number, stato="done",
                                      motivo="raccolta_parziale")
    await db_session.commit()
    # started_at ha default a livello Python: due righe create nello stesso
    # microsecondo romperebbero l'ordinamento. Le si separa esplicitamente.
    vecchia.started_at = vecchia.started_at.replace(year=2020)
    await db_session.commit()

    righe = await wa_discover_runs.storico(db_session, number.id, limit=10)
    assert [r.id for r in righe] == [recente.id, vecchia.id]
```

- [ ] **Step 2: Esegui il test e verifica che fallisca**

Run: `cd backend && ./venv/Scripts/python.exe -m pytest tests/test_wa_discover_runs_servizio.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'app.services.wa_discover_runs'`

- [ ] **Step 3: Scrivi il servizio**

Crea `backend/app/services/wa_discover_runs.py`:

```python
"""Ciclo di vita di una scansione auto-discover.

Una run e' l'unica traccia di com'e' andato uno scan. Il motore
(esegui_discover_run) non la conosce: apre e chiude chi lo lancia, cosi' il
motore resta quello gia' collaudato contro il DOM vero.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import desc, select

from app.models.wa import WaDiscoverRun

# I motivi che il motore restituisce e che NON sono un guasto nostro: la run
# si chiude 'done' anche con questi, perche' il sistema ha fatto la cosa
# giusta (non ha scansionato, e ha detto perche').
MOTIVI_NON_GUASTO = {
    "completato", "raccolta_parziale", "fermato_dopo_stallo",
    "sync_sotto_soglia", "sync_ignota", "sidebar_coperta", "wa_halted",
    "numero_non_attivo", "profilo_occupato", "sessione_non_loggata",
}


def calcola_copertura(esito: dict) -> int | None:
    """Percentuale di lista coperta, 0-100, o None se non calcolabile.

    I salti dell'incrementale contano come raccolto: quelle chat le abbiamo
    gia', non ripagarle e' il punto. Senza includerli, ogni riscansione
    riuscita sembrerebbe una raccolta al 2%.
    """
    dichiarato = esito.get("dichiarato")
    if not dichiarato or dichiarato <= 0:
        return None
    coperte = (esito.get("salvate", 0) + esito.get("aggiornate", 0)
               + esito.get("saltate_gia_note", 0))
    return min(100, round(coperte * 100 / dichiarato))


async def apri_run(db, *, tenant_id: str, number_id: str,
                   avviato_da: str = "manuale") -> WaDiscoverRun:
    run = WaDiscoverRun(tenant_id=tenant_id, number_id=number_id,
                        avviato_da=avviato_da, stato="running", motivo="in_corso")
    db.add(run)
    await db.flush()
    return run


async def chiudi_run(db, run_id: str, esito: dict, *, errore: str | None = None) -> None:
    """Scrive l'esito e chiude la run. Idempotente: una run gia' chiusa non
    viene toccata -- il worker puo' chiamare questa due volte (percorso
    normale + guardia nel finally) e la seconda non deve cancellare la prima.
    """
    run = await db.scalar(select(WaDiscoverRun).where(WaDiscoverRun.id == run_id))
    if run is None or run.stato != "running":
        return

    if errore is not None:
        run.stato = "failed"
        run.motivo = "errore_imprevisto"
        run.errore = errore[:2000]
    else:
        motivo = esito.get("motivo", "completato")
        run.stato = "done" if motivo in MOTIVI_NON_GUASTO else "failed"
        run.motivo = motivo
        run.salvate = esito.get("salvate", 0)
        run.aggiornate = esito.get("aggiornate", 0)
        run.saltate_gia_note = esito.get("saltate_gia_note", 0)
        run.non_verificate = esito.get("non_verificate", 0)
        run.dichiarato = esito.get("dichiarato")
        run.copertura = calcola_copertura(esito)
        run.sync_letta = esito.get("sync_letta")
        run.sync_stato = esito.get("sync_stato", "ignota")

    run.finished_at = datetime.utcnow()
    await db.flush()


async def run_attiva(db, number_id: str) -> WaDiscoverRun | None:
    return await db.scalar(select(WaDiscoverRun).where(
        WaDiscoverRun.number_id == number_id, WaDiscoverRun.stato == "running"))


async def ultima_run(db, number_id: str) -> WaDiscoverRun | None:
    return await db.scalar(
        select(WaDiscoverRun).where(WaDiscoverRun.number_id == number_id)
        .order_by(desc(WaDiscoverRun.started_at)).limit(1))


async def storico(db, number_id: str, *, limit: int = 10) -> list[WaDiscoverRun]:
    righe = await db.execute(
        select(WaDiscoverRun).where(WaDiscoverRun.number_id == number_id)
        .order_by(desc(WaDiscoverRun.started_at)).limit(limit))
    return list(righe.scalars().all())
```

- [ ] **Step 4: Esegui i test e verifica che passino**

Run: `cd backend && ./venv/Scripts/python.exe -m pytest tests/test_wa_discover_runs_servizio.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/wa_discover_runs.py backend/tests/test_wa_discover_runs_servizio.py
git commit -m "feat(wa): servizio del ciclo di vita delle run di discover"
```

---

## Task 3: Le sei guardie pre-lancio

**Files:**
- Create: `backend/app/services/wa_discover_gate.py`
- Modify: `backend/app/services/wa_profile_lock.py`
- Modify: `backend/app/config.py`
- Modify: `backend/requirements.txt`
- Test: `backend/tests/test_wa_discover_gate.py`

**Interfaces:**
- Consumes: `run_attiva` (Task 2)
- Produces:
  - `async def profilo_occupato_da() -> str | None` in `wa_profile_lock`
  - `async def puo_lanciare(db, number) -> str | None` in `wa_discover_gate` — ritorna il codice del rifiuto, o `None` se si può partire
  - `def ram_libera_mb() -> int` in `wa_discover_gate`
  - Costante `MESSAGGI: dict[str, str]` in `wa_discover_gate`

- [ ] **Step 1: Dichiara psutil**

`psutil` è usato dal venv ma **non è in `requirements.txt`**: un import non dichiarato rompe la *collection* di pytest in CI, non un singolo test. Aggiungi in ordine alfabetico in `backend/requirements.txt` (fra `python-multipart` e `redis`):

```
psutil==7.2.2
```

- [ ] **Step 2: Scrivi il test che fallisce**

Crea `backend/tests/test_wa_discover_gate.py`:

```python
import pytest

from app.models.wa import WaNumberStatus
from app.services import wa_discover_gate, wa_discover_runs
from tests.factories_wa import make_number, make_tenant


@pytest.fixture
def gate_pulito(monkeypatch):
    """Tutte le condizioni esterne al verde: ogni test rompe la sua e basta."""
    monkeypatch.setattr(wa_discover_gate.bot_state_service, "is_wa_halted",
                        _async_return(False))
    monkeypatch.setattr(wa_discover_gate.wa_profile_lock, "profilo_occupato_da",
                        _async_return(None))
    monkeypatch.setattr(wa_discover_gate, "ram_libera_mb", lambda: 4000)


def _async_return(valore):
    async def _f(*a, **kw):
        return valore
    return _f


@pytest.mark.asyncio
async def test_verde_quando_tutto_e_a_posto(db_session, gate_pulito):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    assert await wa_discover_gate.puo_lanciare(db_session, number) is None


@pytest.mark.asyncio
async def test_numero_non_attivo(db_session, gate_pulito):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant, status=WaNumberStatus.pending_qr)
    assert await wa_discover_gate.puo_lanciare(db_session, number) == "numero_non_attivo"


@pytest.mark.asyncio
async def test_kill_switch_di_canale(db_session, gate_pulito, monkeypatch):
    monkeypatch.setattr(wa_discover_gate.bot_state_service, "is_wa_halted",
                        _async_return(True))
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    assert await wa_discover_gate.puo_lanciare(db_session, number) == "canale_fermo"


@pytest.mark.asyncio
async def test_browser_occupato_da_UN_ALTRO_numero(db_session, gate_pulito, monkeypatch):
    # Il gate e' GLOBALE: i lock sono per-numero e non si escludono fra loro,
    # ma due browser insieme sono 2,4 GB su una macchina che ne ha 7,5.
    monkeypatch.setattr(wa_discover_gate.wa_profile_lock, "profilo_occupato_da",
                        _async_return("un-altro-numero"))
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    assert await wa_discover_gate.puo_lanciare(db_session, number) == "browser_occupato"


@pytest.mark.asyncio
async def test_browser_occupato_dal_numero_stesso(db_session, gate_pulito, monkeypatch):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    monkeypatch.setattr(wa_discover_gate.wa_profile_lock, "profilo_occupato_da",
                        _async_return(number.id))
    assert await wa_discover_gate.puo_lanciare(db_session, number) == "browser_occupato"


@pytest.mark.asyncio
async def test_scan_gia_in_corso(db_session, gate_pulito):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id, number_id=number.id)
    await db_session.commit()
    assert await wa_discover_gate.puo_lanciare(db_session, number) == "scan_gia_in_corso"


@pytest.mark.asyncio
async def test_ram_insufficiente(db_session, gate_pulito, monkeypatch):
    monkeypatch.setattr(wa_discover_gate, "ram_libera_mb", lambda: 300)
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    assert await wa_discover_gate.puo_lanciare(db_session, number) == "ram_insufficiente"


def test_ogni_codice_di_rifiuto_ha_un_messaggio_per_un_umano():
    # Un 409 senza frase diventa "Errore 409" a schermo, che non dice a
    # nessuno cosa fare dopo.
    for codice in ("numero_non_attivo", "canale_fermo", "browser_occupato",
                   "scan_gia_in_corso", "ram_insufficiente"):
        assert codice in wa_discover_gate.MESSAGGI
        assert len(wa_discover_gate.MESSAGGI[codice]) > 20
```

- [ ] **Step 3: Esegui il test e verifica che fallisca**

Run: `cd backend && ./venv/Scripts/python.exe -m pytest tests/test_wa_discover_gate.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'app.services.wa_discover_gate'`

- [ ] **Step 4: Aggiungi `profilo_occupato_da` al lock**

In `backend/app/services/wa_profile_lock.py`, dopo `release_stale` (riga 118 e seguenti), con lo stesso stile di pool usa-e-getta:

```python
async def profilo_occupato_da() -> str | None:
    """Il number_id di UN profilo con il lucchetto preso, o None se nessuno.

    Serve al gate globale del discover: i lock sono per-numero e non si
    escludono fra loro, ma i browser condividono la RAM della macchina. Su
    questo PC (7,5 GB, 1,2 GB per profilo) due sessioni insieme sono la
    condizione in cui il 14/08 sender e scan hanno girato sovrapposti.

    Sola lettura: non prende, non rilascia, non rinnova nulla.
    """
    redis = await arq.create_pool(arq_redis_settings())
    try:
        async for key in redis.scan_iter(match="wa:profile-lock:*"):
            nome = key.decode() if isinstance(key, bytes) else key
            return nome.split("wa:profile-lock:", 1)[1]
        return None
    finally:
        await redis.aclose()
```

- [ ] **Step 5: Aggiungi le impostazioni**

In `backend/app/config.py`, accanto alle altre `wa_*` (dopo `wa_reply_scan_window_days` alla riga 523):

```python
    # Un profilo WhatsApp costa ~1,2 GB misurati (M0). Sotto questa soglia il
    # discover non parte: il caso peggiore non e' uno scan lento, e' l'OOM che
    # uccide a meta' la mini-sessione d'invio che sta girando accanto.
    wa_discover_ram_min_mb: int = 1500
    # Quante run mostrare nello storico di /wa/scoperti.
    wa_discover_storico_limit: int = 10
```

- [ ] **Step 6: Scrivi il gate**

Crea `backend/app/services/wa_discover_gate.py`:

```python
"""Le guardie che decidono se una scansione puo' partire adesso.

Tutte fail-closed e in ordine dalla piu' economica alla piu' costosa. Il
chiamante riceve un CODICE, non un booleano: la UI deve poter dire perche' no,
e "Errore 409" non dice a nessuno cosa fare dopo.
"""
from __future__ import annotations

import psutil

from app.config import settings
from app.models.wa import WaNumberStatus
from app.services import bot_state_service, wa_discover_runs, wa_profile_lock

MESSAGGI = {
    "numero_non_attivo": (
        "Il numero non e' attivo: collegalo con Avvia login QR prima di scansionare."),
    "canale_fermo": (
        "Il canale WhatsApp e' fermo (kill-switch alzato): riprendilo dalla "
        "striscia in alto prima di scansionare."),
    "browser_occupato": (
        "Un altro numero sta gia' usando il browser sulla macchina del backend. "
        "Su questo PC ne gira uno solo per volta: riprova fra qualche minuto."),
    "scan_gia_in_corso": (
        "Una scansione su questo numero e' gia' in corso: aspetta che finisca."),
    "ram_insufficiente": (
        "Memoria insufficiente per aprire un browser: chiudi qualche finestra "
        "e riprova."),
}


def ram_libera_mb() -> int:
    """RAM disponibile in MB. Funzione a se' per poterla sostituire nei test."""
    return int(psutil.virtual_memory().available / (1024 * 1024))


async def puo_lanciare(db, number) -> str | None:
    """None se si puo' partire, altrimenti il codice del rifiuto."""
    if number.status != WaNumberStatus.active:
        return "numero_non_attivo"

    if await bot_state_service.is_wa_halted(db):
        return "canale_fermo"

    # Gate GLOBALE, non per-numero: vale anche se il lucchetto e' di un altro
    # numero, perche' la risorsa scarsa e' la RAM della macchina.
    if await wa_profile_lock.profilo_occupato_da() is not None:
        return "browser_occupato"

    if await wa_discover_runs.run_attiva(db, number.id) is not None:
        return "scan_gia_in_corso"

    if ram_libera_mb() < settings.wa_discover_ram_min_mb:
        return "ram_insufficiente"

    return None
```

- [ ] **Step 7: Esegui i test e verifica che passino**

Run: `cd backend && ./venv/Scripts/python.exe -m pytest tests/test_wa_discover_gate.py -v`
Expected: 8 passed

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/wa_discover_gate.py backend/app/services/wa_profile_lock.py backend/app/config.py backend/requirements.txt backend/tests/test_wa_discover_gate.py
git commit -m "feat(wa): guardie pre-lancio del discover, col gate globale del browser"
```

---

## Task 4: Job ARQ

**Files:**
- Create: `backend/app/workers/wa_discover_worker.py`
- Modify: `backend/app/workers/task_queue.py`
- Test: `backend/tests/test_wa_discover_worker.py`

**Interfaces:**
- Consumes: `apri_run`, `chiudi_run` (Task 2), `esegui_discover_run` (esistente)
- Produces:
  - `def wa_discover_job_id(run_id: str) -> str`
  - `async def wa_discover_task(ctx: dict, number_id: str, run_id: str) -> None`
  - `async def enqueue_wa_discover(number_id: str, run_id: str) -> bool`

- [ ] **Step 1: Scrivi il test che fallisce**

Crea `backend/tests/test_wa_discover_worker.py`:

```python
import pytest

from app.services import wa_discover_runs
from app.workers import wa_discover_worker
from tests.factories_wa import make_number, make_tenant


def test_il_job_id_contiene_il_run_id_non_il_number_id():
    # enqueue_wa_workers usa wa:send:{number_id} deterministico, e ARQ scarta
    # in silenzio il duplicato: accodati 0, nessun errore. Legando l'id alla
    # run, ogni scansione e' un job distinto e quel guasto muto non si ripete.
    assert wa_discover_worker.wa_discover_job_id("run-abc") == "wa:discover:run-abc"
    assert "run-abc" in wa_discover_worker.wa_discover_job_id("run-abc")


@pytest.mark.asyncio
async def test_il_task_chiude_la_run_con_l_esito_del_motore(db_session, monkeypatch):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    run = await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id,
                                          number_id=number.id)
    await db_session.commit()

    async def finto_motore(number_id, **kw):
        return {"salvate": 12, "aggiornate": 1, "saltate_gia_note": 40,
                "non_verificate": 0, "dichiarato": 60, "motivo": "completato"}

    monkeypatch.setattr(wa_discover_worker, "esegui_discover_run", finto_motore)
    monkeypatch.setattr(wa_discover_worker, "AsyncSessionLocal",
                        _sessione_finta(db_session))

    await wa_discover_worker.wa_discover_task({}, number.id, run.id)

    chiusa = await wa_discover_runs.ultima_run(db_session, number.id)
    assert chiusa.stato == "done"
    assert chiusa.salvate == 12
    assert chiusa.copertura == 88


@pytest.mark.asyncio
async def test_se_il_motore_solleva_la_run_finisce_in_failed(db_session, monkeypatch):
    # esegui_discover_run oggi non solleva mai, ma la run non deve restare
    # 'running' per sempre se un giorno lo facesse: una run appesa blocca
    # ogni scansione futura su quel numero (unique parziale).
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    run = await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id,
                                          number_id=number.id)
    await db_session.commit()

    async def motore_che_esplode(number_id, **kw):
        raise RuntimeError("browser sparito")

    monkeypatch.setattr(wa_discover_worker, "esegui_discover_run", motore_che_esplode)
    monkeypatch.setattr(wa_discover_worker, "AsyncSessionLocal",
                        _sessione_finta(db_session))

    await wa_discover_worker.wa_discover_task({}, number.id, run.id)

    chiusa = await wa_discover_runs.ultima_run(db_session, number.id)
    assert chiusa.stato == "failed"
    assert "browser sparito" in chiusa.errore


def test_il_task_e_registrato_nel_worker():
    # Un job non registrato viene accodato e non parte mai: la run resta
    # 'running' e il numero non e' piu' scansionabile.
    from app.workers.task_queue import WorkerSettings

    nomi = {getattr(f, "__name__", getattr(f, "coroutine", None) and f.coroutine.__name__)
            for f in WorkerSettings.functions}
    assert "wa_discover_task" in nomi


def _sessione_finta(db_session):
    class _Ctx:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *a):
            return False

    return lambda: _Ctx()
```

- [ ] **Step 2: Esegui il test e verifica che fallisca**

Run: `cd backend && ./venv/Scripts/python.exe -m pytest tests/test_wa_discover_worker.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'app.workers.wa_discover_worker'`

- [ ] **Step 3: Scrivi il worker**

Crea `backend/app/workers/wa_discover_worker.py`:

```python
"""Job ARQ della scansione auto-discover.

Il browser vive nel worker come ogni altro browser di questo progetto: dentro
uvicorn non reggerebbe (su Windows --reload spegne Playwright, e ogni riavvio
perderebbe lo scan a meta' lasciando il lucchetto preso).
"""
from __future__ import annotations

import arq
from loguru import logger

from app.database import AsyncSessionLocal
from app.services import wa_discover_runs
from app.services.wa_discover_run import esegui_discover_run
from app.services.work_enqueue import arq_redis_settings


def wa_discover_job_id(run_id: str) -> str:
    """Un job per RUN, non per numero.

    wa_send_job_id lega l'id al number_id e conta sul fatto che ARQ scarti il
    duplicato -- li' e' voluto (max 1 campagna running per numero). Qui no: due
    scansioni successive sullo stesso numero sono due job legittimi, e con un
    id per-numero la seconda verrebbe scartata in silenzio (accodati 0, nessun
    errore) lasciando una run 'running' che non parte mai.
    """
    return f"wa:discover:{run_id}"


async def enqueue_wa_discover(number_id: str, run_id: str) -> bool:
    redis = await arq.create_pool(arq_redis_settings())
    try:
        job = await redis.enqueue_job("wa_discover_task", number_id, run_id,
                                      _job_id=wa_discover_job_id(run_id))
        return job is not None
    finally:
        await redis.aclose()


async def wa_discover_task(ctx: dict, number_id: str, run_id: str) -> None:
    """Esegue un giro di scan e chiude la run. Non solleva mai: un'eccezione
    che risalisse lascerebbe la run 'running' per sempre, e l'indice unico
    parziale renderebbe il numero non piu' scansionabile.
    """
    errore = None
    esito: dict = {}
    try:
        esito = await esegui_discover_run(number_id)
    except Exception as exc:  # noqa: BLE001 -- vedi docstring
        logger.exception(f"[WaDiscover] job {run_id} su {number_id}: {exc}")
        errore = f"{type(exc).__name__}: {exc}"

    async with AsyncSessionLocal() as db:
        await wa_discover_runs.chiudi_run(db, run_id, esito, errore=errore)
        await db.commit()

    logger.info(f"[WaDiscover] job {run_id} su {number_id} chiuso: "
                f"{esito.get('motivo', 'errore_imprevisto')}")
```

- [ ] **Step 4: Registra il job**

In `backend/app/workers/task_queue.py`, aggiungi l'import in cima accanto agli altri worker:

```python
from app.workers.wa_discover_worker import wa_discover_task
```

e la voce in fondo a `WorkerSettings.functions`, dopo `run_organic_session_task`:

```python
        # Scansione auto-discover: un colpo solo, nessun Retry(defer) interno
        # (a differenza di wa_send_task, che rischedula fra mini-sessioni),
        # quindi il max_tries di default basta.
        wa_discover_task,
```

- [ ] **Step 5: Esegui i test e verifica che passino**

Run: `cd backend && ./venv/Scripts/python.exe -m pytest tests/test_wa_discover_worker.py -v`
Expected: 4 passed (il blocco contiene 4 funzioni test_* piu un helper _sessione_finta, che pytest non colleziona)

- [ ] **Step 6: Commit**

```bash
git add backend/app/workers/wa_discover_worker.py backend/app/workers/task_queue.py backend/tests/test_wa_discover_worker.py
git commit -m "feat(wa): job ARQ della scansione, con job id legato alla run"
```

---

## Task 5: Endpoint di lancio e di stato

**Files:**
- Modify: `backend/app/api/wa_numbers.py`
- Test: `backend/tests/test_wa_discover_launch_api.py`

**Interfaces:**
- Consumes: `puo_lanciare`/`MESSAGGI` (Task 3), `apri_run`/`ultima_run`/`storico` (Task 2), `enqueue_wa_discover` (Task 4)
- Produces:
  - `POST /api/wa/numbers/{number_id}/discover` → `{"run_id": str, "queued": bool}` · 404 · 409
  - `GET /api/wa/numbers/{number_id}/discover` → `{"ultima": dict | None, "storico": list[dict], "in_corso": bool}`

- [ ] **Step 1: Scrivi il test che fallisce**

Crea `backend/tests/test_wa_discover_launch_api.py`:

```python
from datetime import datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api import wa_numbers
from app.database import get_db
from app.main import app
from app.models.user import User
from app.models.wa import WaNumberStatus
from app.services import wa_discover_runs
from app.utils.auth_deps import get_current_user
from tests.factories_wa import make_discover_run, make_number, make_tenant


def _admin_utente() -> User:
    return User(id="00000000-0000-0000-0000-00000000000b", email="admin-wa-launch@test.local",
                password_hash="x", role="admin", is_active=True,
                created_at=datetime.utcnow())


@pytest_asyncio.fixture
async def client(db_session):
    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _admin_utente
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def gate_verde(monkeypatch):
    async def _verde(db, number):
        return None

    accodati = []

    async def _enqueue(number_id, run_id):
        accodati.append((number_id, run_id))
        return True

    monkeypatch.setattr(wa_numbers.wa_discover_gate, "puo_lanciare", _verde)
    monkeypatch.setattr(wa_numbers, "enqueue_wa_discover", _enqueue)
    return accodati


@pytest.mark.asyncio
async def test_post_su_numero_inesistente_404(client, gate_verde):
    r = await client.post("/api/wa/numbers/00000000-0000-0000-0000-000000000000/discover")
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_post_apre_la_run_e_accoda(db_session, client, gate_verde):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await db_session.commit()

    r = await client.post(f"/api/wa/numbers/{number.id}/discover")
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["queued"] is True and corpo["run_id"]
    assert gate_verde == [(number.id, corpo["run_id"])]

    attiva = await wa_discover_runs.run_attiva(db_session, number.id)
    assert attiva is not None and attiva.id == corpo["run_id"]
    assert attiva.avviato_da == "manuale"


@pytest.mark.asyncio
@pytest.mark.parametrize("codice", [
    "numero_non_attivo", "canale_fermo", "browser_occupato",
    "scan_gia_in_corso", "ram_insufficiente",
])
async def test_ogni_rifiuto_e_409_con_la_sua_frase(db_session, client, monkeypatch, codice):
    async def _rifiuta(db, number):
        return codice

    accodati = []

    async def _enqueue(number_id, run_id):
        accodati.append(run_id)
        return True

    monkeypatch.setattr(wa_numbers.wa_discover_gate, "puo_lanciare", _rifiuta)
    monkeypatch.setattr(wa_numbers, "enqueue_wa_discover", _enqueue)

    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await db_session.commit()

    r = await client.post(f"/api/wa/numbers/{number.id}/discover")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["codice"] == codice
    assert len(r.json()["detail"]["messaggio"]) > 20
    # Rifiutato significa: nessuna run aperta e nessun job accodato.
    assert accodati == []
    assert await wa_discover_runs.run_attiva(db_session, number.id) is None


@pytest.mark.asyncio
async def test_se_l_accodamento_fallisce_la_run_non_resta_appesa(db_session, client, monkeypatch):
    # Una run 'running' che nessun job chiudera' mai rende il numero non piu'
    # scansionabile (indice unico parziale): va chiusa subito.
    async def _verde(db, number):
        return None

    async def _enqueue_ko(number_id, run_id):
        return False

    monkeypatch.setattr(wa_numbers.wa_discover_gate, "puo_lanciare", _verde)
    monkeypatch.setattr(wa_numbers, "enqueue_wa_discover", _enqueue_ko)

    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await db_session.commit()

    r = await client.post(f"/api/wa/numbers/{number.id}/discover")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["codice"] == "accodamento_fallito"
    assert await wa_discover_runs.run_attiva(db_session, number.id) is None


@pytest.mark.asyncio
async def test_get_senza_nessuna_run(db_session, client):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await db_session.commit()

    r = await client.get(f"/api/wa/numbers/{number.id}/discover")
    assert r.status_code == 200, r.text
    assert r.json() == {"ultima": None, "storico": [], "in_corso": False}


@pytest.mark.asyncio
async def test_get_espone_ultima_storico_e_in_corso(db_session, client):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await make_discover_run(db_session, tenant, number, stato="done", salvate=78,
                            dichiarato=900, copertura=9, motivo="fermato_dopo_stallo")
    await make_discover_run(db_session, tenant, number)  # running
    await db_session.commit()

    r = await client.get(f"/api/wa/numbers/{number.id}/discover")
    corpo = r.json()
    assert corpo["in_corso"] is True
    assert corpo["ultima"]["stato"] == "running"
    assert len(corpo["storico"]) == 2
    chiusa = [s for s in corpo["storico"] if s["stato"] == "done"][0]
    assert (chiusa["salvate"], chiusa["dichiarato"], chiusa["copertura"]) == (78, 900, 9)
    assert chiusa["motivo"] == "fermato_dopo_stallo"


@pytest.mark.asyncio
async def test_get_su_numero_inesistente_404(client):
    r = await client.get("/api/wa/numbers/00000000-0000-0000-0000-000000000000/discover")
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_numero_non_attivo_rifiutato_dal_gate_vero(db_session, client, monkeypatch):
    # Senza mock del gate: la guardia sullo stato deve reggere da sola.
    async def _enqueue(number_id, run_id):
        return True

    monkeypatch.setattr(wa_numbers, "enqueue_wa_discover", _enqueue)
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant, status=WaNumberStatus.retired)
    await db_session.commit()

    r = await client.post(f"/api/wa/numbers/{number.id}/discover")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["codice"] == "numero_non_attivo"
```

- [ ] **Step 2: Esegui il test e verifica che fallisca**

Run: `cd backend && ./venv/Scripts/python.exe -m pytest tests/test_wa_discover_launch_api.py -v`
Expected: FAIL — `AttributeError: module 'app.api.wa_numbers' has no attribute 'wa_discover_gate'`

- [ ] **Step 3: Scrivi gli endpoint**

In `backend/app/api/wa_numbers.py`, aggiungi agli import in cima:

```python
from app.services import wa_discover_gate, wa_discover_runs
from app.workers.wa_discover_worker import enqueue_wa_discover
```

e in fondo al file. Riferimenti verificati: il router ha già `prefix="/wa/numbers"` (riga 27), l'helper `_numero_o_404(db, number_id)` esiste alla riga 146, e `settings` è già importato (riga 19) — nessun import da aggiungere per quello.

```python
def _serializza_run(run) -> dict:
    return {
        "id": run.id,
        "stato": run.stato,
        "avviato_da": run.avviato_da,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "salvate": run.salvate,
        "aggiornate": run.aggiornate,
        "saltate_gia_note": run.saltate_gia_note,
        "non_verificate": run.non_verificate,
        "dichiarato": run.dichiarato,
        "copertura": run.copertura,
        "motivo": run.motivo,
        "sync_stato": run.sync_stato,
        "errore": run.errore,
    }


@router.post("/{number_id}/discover")
async def avvia_discover(number_id: str, db=Depends(get_db)) -> dict:
    """Lancia una scansione auto-discover sul numero.

    Rifiuta invece di accodare: nessuno stato differito, nessun browser che si
    apre da solo mezz'ora dopo quando nessuno guarda. Il codice del rifiuto va
    in `detail` insieme alla frase da mostrare -- "Errore 409" non dice a
    nessuno cosa fare dopo.
    """
    numero = await _numero_o_404(db, number_id)

    rifiuto = await wa_discover_gate.puo_lanciare(db, numero)
    if rifiuto is not None:
        raise HTTPException(409, {"codice": rifiuto,
                                  "messaggio": wa_discover_gate.MESSAGGI[rifiuto]})

    run = await wa_discover_runs.apri_run(db, tenant_id=numero.tenant_id,
                                          number_id=number_id)
    await db.commit()

    if not await enqueue_wa_discover(number_id, run.id):
        # ARQ ha scartato l'accodamento: la run non verra' mai chiusa da
        # nessuno, e l'indice unico parziale renderebbe il numero non piu'
        # scansionabile. Si chiude subito.
        await wa_discover_runs.chiudi_run(db, run.id, {},
                                          errore="accodamento ARQ rifiutato")
        await db.commit()
        raise HTTPException(409, {
            "codice": "accodamento_fallito",
            "messaggio": ("La coda dei job ha rifiutato la scansione. "
                          "Verifica che il worker ARQ sia in esecuzione."),
        })

    return {"run_id": run.id, "queued": True}


@router.get("/{number_id}/discover")
async def stato_discover(number_id: str, db=Depends(get_db)) -> dict:
    await _numero_o_404(db, number_id)
    ultima = await wa_discover_runs.ultima_run(db, number_id)
    righe = await wa_discover_runs.storico(
        db, number_id, limit=settings.wa_discover_storico_limit)
    return {
        "ultima": _serializza_run(ultima) if ultima else None,
        "storico": [_serializza_run(r) for r in righe],
        "in_corso": ultima is not None and ultima.stato == "running",
    }
```

`settings` è già importato alla riga 19: non aggiungerlo una seconda volta.

- [ ] **Step 4: Esegui i test e verifica che passino**

Run: `cd backend && ./venv/Scripts/python.exe -m pytest tests/test_wa_discover_launch_api.py -v`
Expected: 12 passed (i 5 parametrizzati contano singolarmente)

- [ ] **Step 5: Esegui l'intera suite WA per verificare che nulla si sia rotto**

Run: `cd backend && ./venv/Scripts/python.exe -m pytest tests/ -k "wa_" -q`
Expected: nessun fallimento. **Non chiudere con `tail`**: leggi la riga di riepilogo per intero.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/wa_numbers.py backend/tests/test_wa_discover_launch_api.py
git commit -m "feat(wa): endpoint POST/GET di lancio e stato della scansione"
```

---

## Task 6: Riscansione incrementale

**Files:**
- Modify: `backend/app/services/wa_discover_run.py` (`DecisioneRiga` righe ~80-87, `_decidi_riga` righe 90-135, `_esegui_scan` righe 138-289)
- Test: `backend/tests/test_wa_discover_incrementale.py`

**Interfaces:**
- Consumes: `WaDiscoveredChat` (esistente)
- Produces: `_decidi_riga(page, grezza, *, titoli_noti: set[str] | None = None)` con `DecisioneRiga.saltata: bool`; `_esegui_scan` restituisce anche `"saltate_gia_note": int`

- [ ] **Step 1: Scrivi il test che fallisce**

Crea `backend/tests/test_wa_discover_incrementale.py`:

```python
import pytest

from app.services import wa_discover_run
from app.services.wa_discover import pannello


class _PaginaFinta:
    """Pagina che registra se qualcuno ha provato ad aprire un pannello."""

    def __init__(self):
        self.aperture = 0


@pytest.fixture
def conta_aperture(monkeypatch):
    aperture = []

    async def _apri(page, titolo):
        aperture.append(titolo)
        # `salvabile` e' una property calcolata da `esito`, NON un campo del
        # costruttore: passarla esploderebbe con TypeError.
        return pannello.EsitoApertura(esito=pannello.ESITO_VERIFICATA,
                                      numero="+393331112223", testo_pannello="")

    monkeypatch.setattr(wa_discover_run.pannello, "apri_e_leggi", _apri)
    return aperture


@pytest.mark.asyncio
async def test_chat_gia_nota_non_apre_il_pannello(conta_aperture):
    decisione = await wa_discover_run._decidi_riga(
        _PaginaFinta(), {"titolo": "Mario Rossi", "titolo_e_numero": False},
        titoli_noti={"Mario Rossi"})

    assert decisione.saltata is True
    assert decisione.riga is None
    assert decisione.ha_aperto is False
    assert conta_aperture == []


@pytest.mark.asyncio
async def test_chat_sconosciuta_apre_il_pannello(conta_aperture):
    decisione = await wa_discover_run._decidi_riga(
        _PaginaFinta(), {"titolo": "Sconosciuto", "titolo_e_numero": False},
        titoli_noti={"Mario Rossi"})

    assert decisione.saltata is False
    assert decisione.riga is not None
    assert conta_aperture == ["Sconosciuto"]


@pytest.mark.asyncio
async def test_chat_col_numero_nel_titolo_si_salta_per_hmac(conta_aperture):
    # Il caso che il primo tentativo sul campo aveva mancato: 194 righe su 241
    # hanno il titolo mascherato a DB, quindi il confronto per titolo non
    # scatta mai. La chiave e' l'hmac.
    from app.utils.phone_pseudonym import hmac_phone

    decisione = await wa_discover_run._decidi_riga(
        _PaginaFinta(), {"titolo": "+39 334 802 8109", "titolo_e_numero": True},
        titoli_noti=set(), hmac_noti={hmac_phone("+393348028109")})

    assert decisione.saltata is True
    assert decisione.riga is None
    assert conta_aperture == []


@pytest.mark.asyncio
async def test_numero_nel_titolo_MAI_visto_non_si_salta(conta_aperture):
    decisione = await wa_discover_run._decidi_riga(
        _PaginaFinta(), {"titolo": "+39 334 802 8109", "titolo_e_numero": True},
        titoli_noti=set(), hmac_noti=set())

    assert decisione.saltata is False
    assert decisione.riga is not None       # risolta dal titolo, senza aprire
    assert conta_aperture == []


@pytest.mark.asyncio
async def test_senza_titoli_noti_si_comporta_come_prima(conta_aperture):
    decisione = await wa_discover_run._decidi_riga(
        _PaginaFinta(), {"titolo": "Chiunque", "titolo_e_numero": False})

    assert decisione.saltata is False
    assert conta_aperture == ["Chiunque"]


@pytest.mark.asyncio
async def test_il_titolo_che_e_gia_il_numero_resta_gratis_e_non_e_un_salto(conta_aperture):
    # Il ramo esistente non deve diventare un "salto": la riga viene salvata,
    # e contarla come saltata falserebbe la copertura.
    decisione = await wa_discover_run._decidi_riga(
        _PaginaFinta(), {"titolo": "+39 333 111 2223", "titolo_e_numero": True},
        titoli_noti=set())

    assert decisione.saltata is False
    assert decisione.riga is not None
    assert conta_aperture == []
```

E il test sul conteggio a livello di scan, nello stesso file:

```python
@pytest.mark.asyncio
async def test_le_righe_note_finiscono_in_saltate_gia_note_non_in_non_verificate(
        db_session, monkeypatch):
    from tests.factories_wa import make_discovered_chat, make_number, make_tenant

    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await make_discovered_chat(db_session, tenant, number, chat_title="Nota")
    await db_session.commit()

    righe = [{"titolo": "Nota", "titolo_e_numero": False},
             {"titolo": "Nuova", "titolo_e_numero": True}]
    await _monta_scan_finto(monkeypatch, righe)

    esito = await wa_discover_run._esegui_scan(
        _PaginaFinta(), db=db_session, tenant_id=tenant.id, number_id=number.id)

    assert esito["saltate_gia_note"] == 1
    assert esito["non_verificate"] == 0
    assert esito["salvate"] == 1


async def _monta_scan_finto(monkeypatch, righe):
    """Sostituisce sidebar, gate e pause: qui si misura il conteggio, non il DOM."""
    async def _scan(page):
        return righe

    async def _totale(page):
        return len(righe)

    async def _scorri(page):
        class _Stato:
            al_fondo = True
        return _Stato()

    async def _percentuale(page):
        return 100

    async def _lista_ok(page):
        return True

    async def _niente(*a, **kw):
        return None

    monkeypatch.setattr(wa_discover_run.sidebar, "scan_sidebar", _scan)
    monkeypatch.setattr(wa_discover_run.sidebar, "totale_dichiarato", _totale)
    monkeypatch.setattr(wa_discover_run.sidebar, "scorri_sidebar", _scorri)
    monkeypatch.setattr(wa_discover_run, "leggi_percentuale", _percentuale)
    monkeypatch.setattr(wa_discover_run, "lista_utilizzabile", _lista_ok)
    monkeypatch.setattr(wa_discover_run.asyncio, "sleep", _niente)
```

Riferimento verificato: `EsitoApertura` (`backend/app/services/wa_discover/pannello.py:78`) è una dataclass con **tre soli campi** — `esito: str`, `numero: str | None`, `testo_pannello: str` — più la property `salvabile`, che vale `True` solo quando `esito == ESITO_VERIFICATA`. Le costanti sono `ESITO_VERIFICATA`, `ESITO_NON_VERIFICATA`, `ESITO_RIGA_ASSENTE` (righe 74-76).

- [ ] **Step 2: Esegui il test e verifica che fallisca**

Run: `cd backend && ./venv/Scripts/python.exe -m pytest tests/test_wa_discover_incrementale.py -v`
Expected: FAIL con `TypeError: _decidi_riga() got an unexpected keyword argument 'titoli_noti'`

- [ ] **Step 3: Aggiungi `saltata` a `DecisioneRiga`**

In `backend/app/services/wa_discover_run.py`, nella dataclass attorno alla riga 80:

```python
    riga: classifica.RigaScoperta | None
    ha_aperto: bool
    # Riga gia' presente in wa_discovered_chats con un numero: non si riapre
    # il pannello. Distinta da riga=None, che significa "provato e fallito".
    saltata: bool = False
```

- [ ] **Step 4: Aggiungi il salto in `_decidi_riga`**

Subito dopo `titolo = grezza.get("titolo")` (riga 105), prima del ramo `titolo_e_numero`:

```python
    # Riscansione incrementale: una chat gia' in staging con il suo numero non
    # si ripaga. Il costo di uno scan non e' nello scorrere, e' nel CLICCARE
    # ogni riga per aprire il pannello -- 12 secondi l'una, misurati il 14/08
    # su PRIMERO MAGAZZINO (78 chat in 16 minuti). Su 900 chat sono ore, e il
    # discover periodico diventerebbe impraticabile.
    #
    # LA CHIAVE E' L'HMAC, NON IL TITOLO. Provato sul campo il 14/08 e
    # fallito: su 241 righe in staging, 194 hanno il titolo MASCHERATO
    # (`+39•••••761`, vincolo P12) perche' il titolo E' il numero, mentre dal
    # DOM quel titolo arriva in chiaro. Confrontando i titoli il salto scatta
    # su 47 righe su 241 invece che su 232 -- cioe' quasi mai, proprio dove
    # servirebbe di piu'. Il titolo resta il ripiego per le chat con un nome
    # vero, che un numero nel titolo non ce l'hanno.
    #
    # Conseguenza dichiarata: un contatto che cambia numero mantenendo lo
    # stesso nome non viene riverificato. Per lo scopo -- trovare chi ci ha
    # scritto di nuovo -- e' accettabile.
    if titolo and titolo in titoli_noti:
        return DecisioneRiga(riga=None, ha_aperto=False, saltata=True)
    if grezza.get("titolo_e_numero"):
        numero_dal_titolo = classifica.numero_dal_titolo(titolo)
        if numero_dal_titolo is not None and hmac_phone(numero_dal_titolo) in hmac_noti:
            return DecisioneRiga(riga=None, ha_aperto=False, saltata=True)
```

e la firma diventa:

```python
async def _decidi_riga(page, grezza: dict, *,
                       titoli_noti: set[str] | None = None,
                       hmac_noti: set[str] | None = None) -> DecisioneRiga:
```

con `titoli_noti = titoli_noti or set()` e `hmac_noti = hmac_noti or set()` come prima riga del corpo, così i 13 test esistenti che chiamano `_decidi_riga(page, grezza)` continuano a passare. `hmac_phone` si importa da `app.utils.phone_pseudonym`, `classifica` è già importato nel modulo.

- [ ] **Step 5: Conta i salti in `_esegui_scan`**

Tre modifiche puntuali:

Nel dizionario iniziale (riga 153-156):

```python
    esito = {
        "salvate": 0, "aggiornate": 0, "saltate_gia_note": 0,
        "non_verificate": 0, "dichiarato": None, "motivo": "completato",
    }
```

Subito dopo `esito["dichiarato"] = await sidebar.totale_dichiarato(page)` (riga 193):

```python
    # Righe gia' in staging CON un numero: quelle senza vanno riprovate, e'
    # proprio il caso in cui il pannello non era arrivato.
    #
    # Si tengono DUE insiemi perche' le due chiavi coprono popolazioni
    # diverse: l'hmac copre le chat il cui titolo e' il numero (194 su 241
    # misurate il 14/08, e il loro chat_title a DB e' mascherato quindi
    # inconfrontabile), il titolo copre quelle con un nome vero (47 su 241).
    # Un titolo mascherato non entra nell'insieme dei titoli: non
    # combacerebbe mai con quello che arriva dal DOM.
    from app.models.wa import WaDiscoveredChat
    noti = await db.execute(
        select(WaDiscoveredChat.chat_title, WaDiscoveredChat.phone_hmac).where(
            WaDiscoveredChat.number_id == number_id,
            WaDiscoveredChat.phone_hmac.is_not(None)))
    coppie = noti.all()
    hmac_noti = {h for _, h in coppie}
    titoli_noti = {t for t, _ in coppie if t and "•" not in t}
    if hmac_noti:
        logger.info(f"[WaDiscover] {number_id}: {len(hmac_noti)} chat gia' note "
                    f"col numero ({len(titoli_noti)} anche per titolo), "
                    "non verranno riaperte")
```

`select` va importato in cima al modulo se non c'è già (oggi è importato dentro `esegui_discover_run`; spostalo fra gli import di modulo).

Nel corpo del `for`, sostituisci le righe 224-233 con:

```python
            decisione = await _decidi_riga(page, grezza, titoli_noti=titoli_noti,
                                           hmac_noti=hmac_noti)
            if decisione.saltata:
                esito["saltate_gia_note"] += 1
            elif decisione.riga is not None:
                stato_salv = await salvataggio.salva_scoperta(
                    db, tenant_id, number_id, decisione.riga)
                if stato_salv == "creata":
                    esito["salvate"] += 1
                else:
                    esito["aggiornate"] += 1
            else:
                esito["non_verificate"] += 1
```

E nel calcolo di fine giro (riga 264) i salti entrano nel raccolto, altrimenti ogni riscansione riuscita verrebbe dichiarata `raccolta_parziale`:

```python
    raccolte = esito["salvate"] + esito["aggiornate"] + esito["saltate_gia_note"]
    dettaglio = (f"{raccolte} chat coperte ({esito['salvate']} nuove, "
                f"{esito['aggiornate']} aggiornate, {esito['saltate_gia_note']} "
                f"gia' note), {esito['non_verificate']} righe non verificate "
                "(si ritentano al giro dopo)")
```

- [ ] **Step 6: Togli la pausa alle righe saltate**

Alla riga 246 il motore aspetta dopo **ogni** riga. Una riga saltata non ha toccato WhatsApp — nessun click, nessuna apertura — e non c'è nessun ritmo da mascherare: pagarla è tempo buttato su una lista da 900. Sostituisci:

```python
            # Le righe saltate non hanno toccato WhatsApp: nessuna pausa da
            # pagare. E' quello che rende rapido il ritorno al punto dove il
            # giro precedente si era fermato, invece di ricamminare la lista
            # a 0,3 secondi per riga.
            if not decisione.saltata:
                await asyncio.sleep(campiona_pausa(zona_pausa("piena", decisione.ha_aperto)))
```

- [ ] **Step 7: Esegui i test e verifica che passino**

Run: `cd backend && ./venv/Scripts/python.exe -m pytest tests/test_wa_discover_incrementale.py tests/test_wa_discover_run.py -v`
Expected: tutti verdi. I 13 test esistenti di `test_wa_discover_run.py` **devono restare verdi**: `titoli_noti` e `hmac_noti` hanno default `None`, quindi il comportamento senza incrementale non cambia.

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/wa_discover_run.py backend/tests/test_wa_discover_incrementale.py
git commit -m "feat(wa): la riscansione salta le chat gia' note col numero"
```

---

## Task 7: Gate di sincronizzazione tri-stato

**Files:**
- Modify: `backend/app/services/wa_discover/sincronizzazione.py` (`leggi_percentuale` righe 116-143, `puo_scansionare` riga ~202)
- Modify: `backend/app/services/wa_discover_run.py` (`_esegui_scan`, gate alle righe 164-171)
- Modify: `docs/whatsapp/wa-dom-catalog.md`
- Test: `backend/tests/test_wa_discover_sync_tristato.py`

**Interfaces:**
- Produces: `async def leggi_sincronizzazione(page) -> LetturaSync` con `LetturaSync(stato: str, percentuale: int | None)`, `stato ∈ {"letta", "assente", "ignota"}`

**Contesto — perché questo task esiste.** Il 14/08, su `PRIMERO MAGAZZINO`, a browser stabile e sessione `logged_in`, il selettore `[aria-label='Impostazioni'], [aria-label='Settings']` non ha trovato niente, due volte in due sessioni distinte. `leggi_percentuale` restituisce `None` e `puo_scansionare` tratta `None` come "procedi". Non è un fail-open prudente: **il gate non ha mai funzionato**.

- [ ] **Step 1: Ricattura il selettore dal DOM vero**

Con un profilo WhatsApp collegato e nessun altro browser attivo, esegui questa sonda (mettila in `backend/scripts/poc_wa/probe_wa_impostazioni.py`):

```python
"""Quali aria-label espone davvero la barra laterale di WhatsApp Web."""
import asyncio
import sys

sys.path.insert(0, r"D:\BOT OUTBOUND\backend")


async def main() -> None:
    number_id = sys.argv[1]
    from sqlalchemy import select

    from app.database import AsyncSessionLocal
    from app.models.wa import WaNumber
    from app.services import wa_profile_lock
    from app.services.wa_session import WHATSAPP_WEB_URL, _open_wa_browser

    async with AsyncSessionLocal() as db:
        numero = await db.scalar(select(WaNumber).where(WaNumber.id == number_id))
        proxy_url = numero.proxy_url

    async with wa_profile_lock.held(number_id):
        async with _open_wa_browser(number_id, headless=True, proxy_url=proxy_url) as ctx:
            page = await ctx.new_page()
            await page.goto(WHATSAPP_WEB_URL, wait_until="commit", timeout=120_000)
            await page.wait_for_timeout(20_000)
            etichette = await page.evaluate(
                "() => [...document.querySelectorAll('[aria-label]')]"
                ".map(e => e.getAttribute('aria-label')).filter(Boolean)")
            for e in sorted(set(etichette)):
                print(repr(e))


asyncio.run(main())
```

Run: `cd backend && ./venv/Scripts/python.exe scripts\poc_wa\probe_wa_impostazioni.py <number_id>`
Annota l'etichetta vera di Impostazioni e aggiorna `_SEL_IMPOSTAZIONI` con **tutte** le varianti trovate. Registra la scoperta in `docs/whatsapp/wa-dom-catalog.md`.

- [ ] **Step 2: Scrivi il test che fallisce**

Crea `backend/tests/test_wa_discover_sync_tristato.py`:

```python
import pytest

from app.services.wa_discover import sincronizzazione


class _Locator:
    def __init__(self, quanti: int):
        self._quanti = quanti
        self.first = self

    async def count(self):
        return self._quanti

    async def click(self, timeout=None):
        return None


class _Pagina:
    def __init__(self, *, impostazioni_presenti: bool, testi: list[str]):
        self._loc = _Locator(1 if impostazioni_presenti else 0)
        self._testi = testi

    def locator(self, _sel):
        return self._loc

    async def wait_for_timeout(self, _ms):
        return None

    async def evaluate(self, _js):
        return self._testi

    async def keyboard_press(self, _tasto):
        return None


@pytest.fixture(autouse=True)
def niente_richiusura(monkeypatch):
    async def _ok(page):
        return True

    monkeypatch.setattr(sincronizzazione, "_richiudi_pannello", _ok)


@pytest.mark.asyncio
async def test_percentuale_presente_stato_letta():
    pagina = _Pagina(impostazioni_presenti=True,
                     testi=["Sincronizzazione dei messaggi piu' recenti 42%"])
    lettura = await sincronizzazione.leggi_sincronizzazione(pagina)
    assert lettura.stato == "letta"
    assert lettura.percentuale == 42


@pytest.mark.asyncio
async def test_impostazioni_aperto_senza_percentuale_stato_assente():
    # Sincronizzazione finita: WhatsApp non mostra piu' nessuna percentuale.
    # Questo e' il caso in cui SI DEVE procedere.
    pagina = _Pagina(impostazioni_presenti=True, testi=["Profilo", "Chat", "Notifiche"])
    lettura = await sincronizzazione.leggi_sincronizzazione(pagina)
    assert lettura.stato == "assente"
    assert lettura.percentuale is None


@pytest.mark.asyncio
async def test_impostazioni_non_trovato_stato_ignota():
    # Il caso del 14/08: il pulsante non c'e' nel DOM. Non significa
    # "sincronizzato", significa "non lo sappiamo".
    pagina = _Pagina(impostazioni_presenti=False, testi=[])
    lettura = await sincronizzazione.leggi_sincronizzazione(pagina)
    assert lettura.stato == "ignota"
    assert lettura.percentuale is None


def test_puo_scansionare_procede_su_assente():
    ok, _ = sincronizzazione.puo_scansionare_lettura(
        sincronizzazione.LetturaSync(stato="assente", percentuale=None), soglia=60)
    assert ok is True


def test_puo_scansionare_si_ferma_su_ignota():
    ok, motivo = sincronizzazione.puo_scansionare_lettura(
        sincronizzazione.LetturaSync(stato="ignota", percentuale=None), soglia=60)
    assert ok is False
    assert "ignota" in motivo or "non" in motivo


def test_puo_scansionare_si_ferma_sotto_soglia():
    ok, _ = sincronizzazione.puo_scansionare_lettura(
        sincronizzazione.LetturaSync(stato="letta", percentuale=42), soglia=60)
    assert ok is False


def test_puo_scansionare_procede_sopra_soglia():
    ok, _ = sincronizzazione.puo_scansionare_lettura(
        sincronizzazione.LetturaSync(stato="letta", percentuale=95), soglia=60)
    assert ok is True
```

- [ ] **Step 3: Esegui il test e verifica che fallisca**

Run: `cd backend && ./venv/Scripts/python.exe -m pytest tests/test_wa_discover_sync_tristato.py -v`
Expected: FAIL con `AttributeError: module ... has no attribute 'leggi_sincronizzazione'`

- [ ] **Step 4: Implementa il tri-stato**

In `backend/app/services/wa_discover/sincronizzazione.py`, accanto alle funzioni esistenti (che restano, per non rompere i 12 test già verdi):

```python
@dataclass(frozen=True)
class LetturaSync:
    """Cosa sappiamo davvero della sincronizzazione.

    Tre stati, non due, perche' `None` da solo confonde "finita" con "non
    lo so" -- ed e' il motivo per cui il 14/08 uno scan e' partito su un
    profilo di cui non sapevamo nulla e ha raccolto 78 righe su 900.
    """
    stato: str          # "letta" | "assente" | "ignota"
    percentuale: int | None


async def leggi_sincronizzazione(page) -> LetturaSync:
    try:
        voce = page.locator(_SEL_IMPOSTAZIONI).first
        if not await voce.count():
            logger.warning(
                "[WaDiscover] voce Impostazioni non trovata: stato di "
                "sincronizzazione IGNOTO (non significa sincronizzato)")
            return LetturaSync(stato="ignota", percentuale=None)
        await voce.click(timeout=4000)
        await page.wait_for_timeout(1500)
        testi = await page.evaluate(_JS_TESTI_PAGINA)
        percentuale = percentuale_da_testi(testi)
        if percentuale is None:
            # Pannello aperto e nessuna percentuale: WhatsApp la mostra solo
            # MENTRE sincronizza. Assente = finita.
            return LetturaSync(stato="assente", percentuale=None)
        return LetturaSync(stato="letta", percentuale=percentuale)
    except Exception as exc:
        logger.warning(f"[WaDiscover] lettura sincronizzazione fallita: {exc}")
        return LetturaSync(stato="ignota", percentuale=None)
    finally:
        await _richiudi_pannello(page)


def puo_scansionare_lettura(lettura: LetturaSync, *, soglia: int) -> tuple[bool, str]:
    if lettura.stato == "assente":
        return True, "sincronizzazione conclusa (nessuna percentuale in Impostazioni)"
    if lettura.stato == "ignota":
        return False, ("stato di sincronizzazione ignota: Impostazioni non "
                       "raggiungibile, non si scansiona alla cieca")
    if lettura.percentuale < soglia:
        return False, (f"sincronizzazione al {lettura.percentuale}%, sotto la "
                       f"soglia del {soglia}%")
    return True, f"sincronizzazione al {lettura.percentuale}%"
```

`dataclass` va importato in cima al modulo.

- [ ] **Step 5: Collega il gate al motore, con attesa e ritentativo**

In `backend/app/services/wa_discover_run.py`, sostituisci le righe 164-171 con:

```python
    # Su "ignota" si aspetta e si riprova invece di procedere: il 14/08 la
    # voce Impostazioni non era nel DOM a 11 secondi dall'apertura, e il gate
    # ha lasciato passare uno scan su un profilo di cui non sapeva nulla.
    lettura = None
    for attesa_s in (0, 5, 15):
        if attesa_s:
            await asyncio.sleep(attesa_s)
        lettura = await leggi_sincronizzazione(page)
        if lettura.stato != "ignota":
            break

    esito["sync_stato"] = lettura.stato
    esito["sync_letta"] = lettura.percentuale
    ok_sync, motivo_sync = puo_scansionare_lettura(lettura, soglia=soglia_sync)
    if not ok_sync:
        logger.info(f"[WaDiscover] {number_id}: scan non avviato -- {motivo_sync}")
        emit_event(number_id, "wa_discover_skipped", motivo_sync, level="warn")
        esito["motivo"] = ("sync_ignota" if lettura.stato == "ignota"
                           else "sync_sotto_soglia")
        return esito
    logger.info(f"[WaDiscover] {number_id}: gate sync ok -- {motivo_sync}")
```

Aggiorna l'import in cima al modulo: `from app.services.wa_discover.sincronizzazione import (leggi_sincronizzazione, lista_utilizzabile, puo_scansionare_lettura)` — mantieni gli import esistenti se altri punti li usano. Aggiungi `"sync_stato": "ignota", "sync_letta": None` al dizionario `esito` iniziale.

- [ ] **Step 6: Esegui i test e verifica che passino**

Run: `cd backend && ./venv/Scripts/python.exe -m pytest tests/test_wa_discover_sync_tristato.py tests/test_wa_discover_sincronizzazione.py tests/test_wa_discover_run.py -v`
Expected: tutti verdi

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/wa_discover/sincronizzazione.py backend/app/services/wa_discover_run.py backend/scripts/poc_wa/probe_wa_impostazioni.py docs/whatsapp/wa-dom-catalog.md backend/tests/test_wa_discover_sync_tristato.py
git commit -m "fix(wa): il gate di sincronizzazione distingue 'sincronizzato' da 'non lo so'"
```

---

## Task 8: Bottone di scansione in `/wa/numeri`

**Files:**
- Modify: `frontend/lib/waApi.ts`
- Modify: `frontend/app/wa/numeri/page.tsx`

**Interfaces:**
- Consumes: `POST`/`GET /wa/numbers/{id}/discover` (Task 5)
- Produces: tipi `WaDiscoverRun`, `WaDiscoverStato`; `waApi.numeri.discover`, `waApi.numeri.discoverStato`

- [ ] **Step 1: Aggiungi tipi e client**

In `frontend/lib/waApi.ts`, accanto agli altri tipi:

```ts
// wa_numbers._serializza_run: una riga di wa_discover_runs. `motivo` sono i
// valori del motore (completato, raccolta_parziale, fermato_dopo_stallo,
// sync_ignota, sync_sotto_soglia, sidebar_coperta, wa_halted,
// numero_non_attivo, profilo_occupato, sessione_non_loggata,
// errore_imprevisto, in_corso): non ri-derivarli lato client.
export type WaDiscoverRun = {
  id: string
  stato: 'running' | 'done' | 'failed'
  avviato_da: 'manuale' | 'cron'
  started_at: string | null
  finished_at: string | null
  salvate: number
  aggiornate: number
  saltate_gia_note: number
  non_verificate: number
  dichiarato: number | null
  copertura: number | null
  motivo: string
  sync_stato: 'letta' | 'assente' | 'ignota'
  errore: string | null
}

export type WaDiscoverStato = {
  ultima: WaDiscoverRun | null
  storico: WaDiscoverRun[]
  in_corso: boolean
}
```

E dentro il namespace `numeri`, dopo `riattiva`:

```ts
    // Apre un browser sulla macchina del backend e blocca gli invii su TUTTI
    // i numeri finche' non finisce: la conferma in UI deve dirlo.
    discover: (id: string) =>
      req<{ run_id: string; queued: boolean }>(`/wa/numbers/${id}/discover`, { method: 'POST' }),
    discoverStato: (id: string) => req<WaDiscoverStato>(`/wa/numbers/${id}/discover`),
```

Il wrapper `req` fa `detail?.detail ?? 'Errore ${status}'`: il nostro 409 ha `detail` come **oggetto**, non stringa. Correggi `req` perché il messaggio arrivi comunque a schermo:

```ts
    const detail = await res.json().catch(() => null)
    const grezzo = detail?.detail
    const messaggio = typeof grezzo === 'string'
      ? grezzo
      // I 409 del discover mandano {codice, messaggio}: senza questo ramo
      // l'utente vedrebbe "[object Object]".
      : (grezzo?.messaggio ?? `Errore ${res.status}`)
    throw new Error(messaggio)
```

- [ ] **Step 2: Aggiungi la colonna e il bottone**

In `frontend/app/wa/numeri/page.tsx`, aggiungi l'intestazione fra "Ultimo check" e "Azioni":

```tsx
                <th className="px-4 py-3 text-left font-medium">Ultimo scan</th>
```

In `RigaNumero`, la cella corrispondente prima di quella delle azioni:

```tsx
        <td className="px-4 py-3"><UltimoScanCella numero={numero} /></td>
```

E il `colSpan` del banner "nessun proxy" passa da `9` a `10`.

Poi aggiungi in fondo al file:

```tsx
// Etichette leggibili per i motivi del motore. Un motivo non mappato si
// mostra grezzo: mai una cella muta.
const MOTIVO_LABEL: Record<string, string> = {
  in_corso: 'in corso',
  completato: 'completo',
  raccolta_parziale: 'raccolta parziale',
  fermato_dopo_stallo: 'fermato dopo stallo',
  sync_ignota: 'sincronizzazione ignota',
  sync_sotto_soglia: 'sincronizzazione incompleta',
  sidebar_coperta: 'lista coperta da un pannello',
  wa_halted: 'canale fermato',
  numero_non_attivo: 'numero non attivo',
  profilo_occupato: 'profilo occupato',
  sessione_non_loggata: 'sessione scaduta',
  errore_imprevisto: 'errore',
}

const MOTIVI_BUONI = new Set(['completato'])

function UltimoScanCella({ numero }: { numero: WaNumber }) {
  const { data } = useSWR(
    `wa-discover-${numero.id}`,
    () => waApi.numeri.discoverStato(numero.id),
    // FAIL-CLOSED, stesso criterio di OrganicSessionButton: si continua a
    // pollare finche' NON si sa che e' finita. Il caso peggiore e' un
    // bottone disabilitato piu' a lungo, non un secondo browser aperto.
    { refreshInterval: (ultimo) => (ultimo?.in_corso ?? true) ? 10_000 : 0 },
  )

  const ultima = data?.ultima
  if (!ultima) return <span style={{ color: 'var(--wa-muted)' }}>Mai</span>
  if (ultima.stato === 'running') {
    return <span style={{ color: 'var(--wa-accent)' }}>In corso...</span>
  }

  const buono = MOTIVI_BUONI.has(ultima.motivo)
  return (
    <div className="text-xs leading-tight">
      <div style={{ color: 'var(--wa-muted)' }}>{formatCheck(ultima.finished_at)}</div>
      <div style={{ color: buono ? 'var(--wa-muted)' : '#e07a3c' }}>
        {ultima.dichiarato
          ? `${ultima.salvate + ultima.aggiornate + ultima.saltate_gia_note}/${ultima.dichiarato}`
          : `${ultima.salvate + ultima.aggiornate}`}
        {ultima.copertura !== null && ` (${ultima.copertura}%)`}
        {' · '}{MOTIVO_LABEL[ultima.motivo] ?? ultima.motivo}
      </div>
    </div>
  )
}

function ScansionaContattiButton({ numero, onChanged }: { numero: WaNumber; onChanged: () => void }) {
  const [open, setOpen] = useState(false)
  const [avvio, setAvvio] = useState(false)
  const { data: stato, mutate: refreshStato } = useSWR(
    `wa-discover-${numero.id}`,
    () => waApi.numeri.discoverStato(numero.id),
    { refreshInterval: (ultimo) => (ultimo?.in_corso ?? true) ? 10_000 : 0 },
  )
  const inCorso = stato?.in_corso ?? false
  const eraInCorso = useRef(false)

  // Un solo toast sulla transizione in-corso -> finita, come
  // OrganicSessionButton: senza questo, ogni giro di polling ne stampa uno.
  useEffect(() => {
    if (eraInCorso.current && stato && !stato.in_corso && stato.ultima) {
      const u = stato.ultima
      const coperte = u.salvate + u.aggiornate + u.saltate_gia_note
      if (u.stato === 'failed') {
        toast.error(`Scansione di ${numero.label} fallita: ${u.errore ?? u.motivo}`)
      } else if (u.motivo === 'completato') {
        toast.success(`${numero.label}: ${coperte} chat coperte, ${u.salvate} nuove.`)
      } else {
        toast.info(
          `${numero.label}: scansione chiusa come "${MOTIVO_LABEL[u.motivo] ?? u.motivo}"`
          + (u.copertura !== null ? ` — copertura ${u.copertura}%.` : '.'))
      }
      onChanged()
    }
    eraInCorso.current = inCorso
  }, [stato, inCorso, numero.label, onChanged])

  const handleConfirm = async () => {
    setAvvio(true)
    try {
      await waApi.numeri.discover(numero.id)
      toast.info(`Scansione avviata per ${numero.label}. Puo' durare parecchi minuti.`)
      await refreshStato()
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
    } finally {
      setAvvio(false)
    }
  }

  return (
    <>
      <Button
        size="sm" variant="outline" type="button" disabled={avvio || inCorso}
        onClick={() => setOpen(true)}
        style={{ borderColor: 'var(--wa-border)', color: 'var(--wa-muted)' }}
      >
        {avvio || inCorso ? 'Scansione in corso...' : 'Scansiona contatti'}
      </Button>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title={`Scansiona i contatti di ${numero.label}`}
        description={"Apre un browser sulla macchina che ospita il backend e legge la lista chat di questo numero. Non invia nulla. Blocca gli invii su TUTTI i numeri finche' non finisce, e su una rubrica grande puo' durare parecchi minuti."}
        confirmLabel="Scansiona"
        variant="warning"
        onConfirm={handleConfirm}
      />
    </>
  )
}
```

Aggiungi il bottone nella cella Azioni di `RigaNumero`, insieme agli altri, con lo stesso gating di stato:

```tsx
            {numero.status === 'active' && (
              <ScansionaContattiButton numero={numero} onChanged={onChanged} />
            )}
```

Aggiorna gli import in cima: `import { useState, useEffect, useRef } from 'react'`.

Le due `useSWR` con la stessa chiave `wa-discover-${numero.id}` condividono la cache SWR: una sola richiesta di rete per riga, non due.

- [ ] **Step 3: Verifica lint e build**

Run (dalla **root del repo**, non dal worktree):
```
cd frontend && npm run lint && npm run build
```
Expected: nessun errore ESLint, build completata.

- [ ] **Step 4: Prova manuale dal browser**

Con backend, worker ARQ e frontend avviati: apri `/wa/numeri`, verifica che la colonna "Ultimo scan" mostri "Mai" su un numero mai scansionato, premi "Scansiona contatti" su un numero **non attivo** e verifica che il toast riporti la frase del 409 (non "Errore 409" né "[object Object]").

- [ ] **Step 5: Commit**

```bash
git add frontend/lib/waApi.ts frontend/app/wa/numeri/page.tsx
git commit -m "feat(wa): bottone Scansiona contatti e colonna Ultimo scan"
```

---

## Task 9: Testata ed esito in `/wa/scoperti`

**Files:**
- Modify: `frontend/app/wa/scoperti/page.tsx`

**Interfaces:**
- Consumes: `waApi.numeri.discoverStato`, `waApi.numeri.discover` (Task 8)

- [ ] **Step 1: Aggiungi la testata**

In `frontend/app/wa/scoperti/page.tsx`, dopo il riquadro dei filtri e prima della tabella, quando `numberId` è valorizzato:

```tsx
{numberId && <TestataScan numberId={numberId} onRiscansionato={refreshScoperti} />}
```

E in fondo al file:

```tsx
const MOTIVO_LABEL_SCAN: Record<string, string> = {
  in_corso: 'in corso',
  completato: 'completo',
  raccolta_parziale: 'raccolta parziale',
  fermato_dopo_stallo: 'fermato dopo stallo',
  sync_ignota: 'sincronizzazione ignota',
  sync_sotto_soglia: 'sincronizzazione incompleta',
  sidebar_coperta: 'lista coperta da un pannello',
  wa_halted: 'canale fermato',
  numero_non_attivo: 'numero non attivo',
  profilo_occupato: 'profilo occupato',
  sessione_non_loggata: 'sessione scaduta',
  errore_imprevisto: 'errore',
}

function TestataScan({ numberId, onRiscansionato }:
    { numberId: string; onRiscansionato: () => void }) {
  const [avvio, setAvvio] = useState(false)
  const [storicoAperto, setStoricoAperto] = useState(false)
  const { data, mutate } = useSWR(
    `wa-discover-${numberId}`,
    () => waApi.numeri.discoverStato(numberId),
    { refreshInterval: (ultimo) => (ultimo?.in_corso ?? true) ? 10_000 : 0 },
  )

  const ultima = data?.ultima
  const inCorso = data?.in_corso ?? false

  async function riscansiona() {
    setAvvio(true)
    try {
      await waApi.numeri.discover(numberId)
      toast.info('Scansione avviata. Puo\' durare parecchi minuti.')
      await mutate()
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : 'Errore')
    } finally {
      setAvvio(false)
    }
  }

  return (
    <Riquadro>
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          {!ultima && (
            <p className="text-sm" style={{ color: 'var(--wa-muted)' }}>
              Questo numero non e&apos; mai stato scansionato.
            </p>
          )}
          {ultima && (
            <>
              <p className="text-sm text-white">
                Ultimo scan {formatData(ultima.finished_at ?? ultima.started_at)}
                {ultima.dichiarato !== null && (
                  <> — {ultima.salvate + ultima.aggiornate + ultima.saltate_gia_note} su{' '}
                    {ultima.dichiarato}
                    {ultima.copertura !== null && ` (${ultima.copertura}%)`}</>
                )}
                {' · '}{MOTIVO_LABEL_SCAN[ultima.motivo] ?? ultima.motivo}
              </p>
              {/* Il sospetto va detto accanto al risultato, non solo nei log:
                  una raccolta corta con la sincronizzazione ignota ha un
                  primo indiziato, e chi guarda la pagina deve saperlo. */}
              {ultima.sync_stato === 'ignota' && ultima.motivo !== 'completato' && (
                <p className="text-xs" style={{ color: '#e07a3c' }}>
                  Sincronizzazione ignota durante lo scan: e&apos; il primo indiziato
                  se la raccolta e&apos; corta.
                </p>
              )}
              {ultima.saltate_gia_note > 0 && (
                <p className="text-xs" style={{ color: 'var(--wa-muted)' }}>
                  {ultima.saltate_gia_note} chat gia&apos; note non sono state riaperte.
                </p>
              )}
            </>
          )}
        </div>
        <div className="flex items-center gap-2">
          {(data?.storico?.length ?? 0) > 0 && (
            <Button type="button" variant="ghost" onClick={() => setStoricoAperto((v) => !v)}
              style={{ color: 'var(--wa-muted)' }}>
              {storicoAperto ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
              Storico
            </Button>
          )}
          <Button type="button" disabled={avvio || inCorso} onClick={riscansiona}
            style={{ backgroundColor: 'var(--wa-accent)', color: '#04120e' }}>
            {avvio || inCorso ? 'Scansione in corso...' : 'Riscansiona'}
          </Button>
        </div>
      </div>

      {storicoAperto && (
        <table className="mt-4 w-full text-xs">
          <thead>
            <tr style={{ color: 'var(--wa-muted)' }}>
              <th className="py-1 text-left font-medium">Quando</th>
              <th className="py-1 text-left font-medium">Avviato da</th>
              <th className="py-1 text-right font-medium">Coperte</th>
              <th className="py-1 text-right font-medium">Nuove</th>
              <th className="py-1 text-right font-medium">Copertura</th>
              <th className="py-1 text-left font-medium">Esito</th>
            </tr>
          </thead>
          <tbody>
            {(data?.storico ?? []).map((r) => (
              <tr key={r.id} style={{ borderTop: '1px solid var(--wa-border)' }}>
                <td className="py-1" style={{ color: 'var(--wa-muted)' }}>
                  {formatData(r.finished_at ?? r.started_at)}
                </td>
                <td className="py-1" style={{ color: 'var(--wa-muted)' }}>{r.avviato_da}</td>
                <td className="py-1 text-right" style={{ color: 'var(--wa-muted)' }}>
                  {r.salvate + r.aggiornate + r.saltate_gia_note}
                </td>
                <td className="py-1 text-right" style={{ color: 'var(--wa-muted)' }}>{r.salvate}</td>
                <td className="py-1 text-right" style={{ color: 'var(--wa-muted)' }}>
                  {r.copertura !== null ? `${r.copertura}%` : '-'}
                </td>
                <td className="py-1" style={{ color: r.motivo === 'completato' ? 'var(--wa-muted)' : '#e07a3c' }}>
                  {MOTIVO_LABEL_SCAN[r.motivo] ?? r.motivo}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Riquadro>
  )
}
```

Aggiungi `waApi` è già importato; verifica che `ChevronDown`/`ChevronRight` siano fra gli import di `lucide-react` (lo sono).

- [ ] **Step 2: Quando uno scan finisce, ricarica la lista**

Dentro `TestataScan`, aggiungi l'effetto che chiama `onRiscansionato` alla transizione, con lo stesso `useRef` del Task 8: le chat nuove devono comparire senza che l'operatore ricarichi la pagina.

```tsx
  const eraInCorso = useRef(false)
  useEffect(() => {
    if (eraInCorso.current && !inCorso) onRiscansionato()
    eraInCorso.current = inCorso
  }, [inCorso, onRiscansionato])
```

- [ ] **Step 3: Verifica lint e build**

Run: `cd frontend && npm run lint && npm run build`
Expected: nessun errore

- [ ] **Step 4: Commit**

```bash
git add frontend/app/wa/scoperti/page.tsx
git commit -m "feat(wa): testata con esito dello scan e storico in /wa/scoperti"
```

---

## Chiusura del modulo

Protocollo obbligatorio della skill `sviluppo-modulo`, nell'ordine:

1. **Lista test manuali UI, minimo 20**, scritta come la eseguirebbe Tommaso, passo per passo. Salvala in `.superpowers/sdd/qa-wa-discover-lancio-tests.md`. Casi che non possono mancare: bottone su numero mai scansionato · su numero già scansionato · durante una campagna che invia (deve rifiutare con la frase giusta) · durante un'altra scansione · su numero `pending_qr`/`retired` · col kill-switch alzato · doppio click ravvicinato · ricarica della pagina a scansione in corso · toast unico a fine scansione · storico che si apre e mostra le run in ordine · riscansione da `/wa/scoperti` che fa comparire le chat nuove senza ricaricare.
2. **Lista adversarial, minimo 30**, in `.superpowers/sdd/qa-wa-discover-lancio-adversarial.md`. Criterio di PASS **invertito**: passa se il sistema si difende. Obbligatorie: due `POST` concorrenti veri con `asyncio.gather` su sessioni DB indipendenti (deve vincerne uno solo — l'indice unico parziale è lì per questo) · `number_id` di un altro tenant · `number_id` malformato, vuoto, di 10k caratteri, con null byte · run lasciata `running` a mano e verifica che il numero non resti bloccato per sempre · worker ARQ spento e `POST` (deve dare `accodamento_fallito` e chiudere la run) · Redis irraggiungibile durante il gate · lock di un altro numero presente · RAM sotto soglia simulata · motivo del motore non mappato in `MOTIVO_LABEL` (la UI non deve restare muta) · `detail` oggetto nel 409 che non deve mai diventare `[object Object]` · invarianti SQL a fine run: nessuna run `running` orfana, nessun `wa_messages` scritto durante uno scan **filtrando per number_id**, nessun numero in chiaro in `wa_discover_runs.errore`.
3. **Fix loop fino al 100%.** "Quasi tutti" = modulo non chiuso.
4. **Review finale dell'intero branch** (`superpowers:requesting-code-review`).
5. **PR verso `main`.** Migrazione `035` da applicare **prima** del riavvio del backend: una colonna mancante è un 500 al primo `GET`.

## Fuori scope (cantiere 2)

Il discover periodico dentro `wa_reply_watcher` (una volta al giorno per numero, solo con nessuna campagna in invio, riusando il browser già aperto), la fermata anticipata dello scroll sulla data dell'ultimo scan, e il frequency cap fra campagne. La presa è lasciata: `avviato_da='cron'` esiste già nel modello e nell'API.
