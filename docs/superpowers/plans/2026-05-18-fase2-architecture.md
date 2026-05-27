# Fase 2 — Architecture & Strategic Plan

> **Codex status 2026-05-18:** implementazione codice completata per i Task 1-11. Verifiche locali OK: `compileall`, import smoke backend/worker/cron, test non-DB, `npm run build`, `npm run lint` (solo warning pre-esistenti). Migrazioni DB `011`/`012` create ma non applicate da questa sessione: la sandbox ha bloccato la connessione Supabase (`WinError 5`). Prima di avviare BE/Worker eseguire da `backend/`: `.\venv\Scripts\python.exe -m scripts.migrate`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`). Questa fase contiene refactor strutturali: eseguire un Task alla volta, con review fra Task. NON iniziare un Task senza aver completato e verificato il precedente.

**Goal:** Rendere il sistema robusto sotto scala e crash: modello queue/worker corretto (decisione utente = combinato a fasi), separazione "contatto lavorato vs prenotazione", migrazioni fuori dal boot API, adapter testabili, osservabilità operativa, ambiente riproducibile.

**Architecture:** Decisioni già fissate in `AUDIT_UNIFICATO.md`:
- Queue: **F1** worker DM short-lived + coda/processo cron dedicato + stop delete `arq:in-progress`; **F2** lease DB + heartbeat + cancel cooperativo.
- Reservation: dopo il fix minimo (Fase 0 C5), introdurre `contact_reservations` + state machine DM esplicita.

**Tech Stack:** Python 3.13, ARQ, SQLAlchemy 2 async, Alembic, FastAPI, Patchright, Next.js.

**Prerequisiti:** Fase 0 e Fase 1 completate (in particolare C5 fix minimo e migrazioni fino a 010).

**Verifica standard:** `compileall`, `pytest`, e per i Task queue un test d'integrazione manuale documentato in ogni Task. Checkout non-git → commit = spuntare checkbox.

---

## File Structure

| File | Responsabilità | Task |
|---|---|---|
| `backend/app/services/human_behavior.py` | Sleep active-hours interrompibile + heartbeat | 1 |
| `backend/app/services/work_enqueue.py`, `backend/app/api/campaigns.py` | Stop delete `arq:in-progress` | 2 |
| `backend/app/services/campaign_orchestrator.py`, `backend/app/workers/message_worker.py` | Worker DM short-lived (batch + re-enqueue) | 3 |
| `backend/app/workers/cron_worker.py` (nuovo), `backend/app/workers/task_queue.py` | Coda/processo cron dedicato | 4 |
| `backend/scripts/migrate.py` (nuovo), `backend/app/main.py` | Migrazioni fuori dal boot API | 5 |
| `backend/alembic/versions/011_contact_reservations.py` (nuovo), `backend/app/models/contact_reservation.py` (nuovo), orchestrator | Reservation table + state machine | 6 |
| `backend/app/services/account_lease.py` (nuovo) | Lease + heartbeat account/job | 7 |
| `backend/app/adapters/` (nuovo) | Interfacce Instagram/AI/browser | 8 |
| `backend/app/api/ops.py` (nuovo), frontend | Osservabilità operativa | 9 |
| `backend/app/services/campaign_orchestrator.py` | M4: claim senza `ORDER BY random()` full-scan | 10 |
| `backend/requirements.txt`, `backend/pyproject.toml`, `frontend`, `.github/` | Lockfile, pin, font offline, CI | 11 |

---

## Task 1: U5 — Sleep active-hours/break interrompibili + heartbeat

**Files:**
- Modify: `backend/app/services/human_behavior.py` (`wait_until_active_hours`)
- Modify: `backend/app/services/campaign_orchestrator.py` (chiamata `wait_until_active_hours`)

- [ ] **Step 1: Rendere `wait_until_active_hours` a chunk con check stato + heartbeat**

In `human_behavior.py` sostituire il corpo di `wait_until_active_hours` con una versione interrompibile che accetta `campaign_id` e `db`:

```python
    async def wait_until_active_hours(self, campaign_id: str | None = None, db=None) -> bool:
        """Sleep a chunk di 30s fino alla finestra attiva. Ritorna False se la
        campagna non è più running/scraping_and_running (interruzione)."""
        from app.models.campaign import Campaign, CampaignStatus
        from sqlalchemy import select, update as sa_update
        from datetime import datetime as _dt
        now_local = datetime.utcnow() + timedelta(hours=settings.timezone_offset_hours)
        if self.is_active_hour():
            return True
        next_start = now_local.replace(hour=settings.active_hours_start,
                                       minute=random.randint(0, 30), second=0)
        if next_start <= now_local:
            next_start = next_start + timedelta(days=1)
        wait_s = (next_start - now_local).total_seconds()
        logger.info(f"Outside active hours — sleeping up to {wait_s/3600:.1f}h (interruptible)")
        elapsed = 0.0
        last_hb = 0.0
        while elapsed < wait_s:
            await asyncio.sleep(min(30.0, wait_s - elapsed))
            elapsed += 30.0
            if db is not None and campaign_id is not None:
                if elapsed - last_hb >= 600.0:
                    try:
                        await db.execute(sa_update(Campaign).where(Campaign.id == campaign_id)
                                         .values(updated_at=_dt.utcnow()))
                        await db.commit()
                        last_hb = elapsed
                    except Exception:
                        pass
                db.expire_all()
                camp = await db.scalar(select(Campaign).where(Campaign.id == campaign_id))
                if not camp or camp.status not in (CampaignStatus.running, CampaignStatus.scraping_and_running):
                    return False
            if self.is_active_hour():
                return True
        return True
```

- [ ] **Step 2: Aggiornare il chiamante nell'orchestrator**

In `campaign_orchestrator.py`, dove c'è:

```python
            if not session_mgr.is_active_hour():
                await _close_browser()
                await session_mgr.wait_until_active_hours()
```

sostituire con:

```python
            if not session_mgr.is_active_hour():
                await _close_browser()
                still = await session_mgr.wait_until_active_hours(campaign_id, db)
                if not still:
                    emit_event(campaign_id, "worker_stopped",
                               "Campagna fermata durante attesa orario attivo", level="warn")
                    return
```

- [ ] **Step 3: Verifica sintassi**

Run (da `backend/`): `./venv/Scripts/python -m compileall app/services/human_behavior.py app/services/campaign_orchestrator.py`
Expected: nessun `SyntaxError`.

- [ ] **Step 4: Commit** (skip se non-git)

```bash
git add backend/app/services/human_behavior.py backend/app/services/campaign_orchestrator.py
git commit -m "fix(worker): interruptible active-hours wait with heartbeat"
```

---

## Task 2: F1 — Smettere di cancellare `arq:in-progress`

**Files:**
- Modify: `backend/app/services/work_enqueue.py`
- Modify: `backend/app/api/campaigns.py` (`delete_campaign`)

- [ ] **Step 1: Helper di pulizia chiavi SICURE (no in-progress)**

In `work_enqueue.py` sostituire i blocchi che fanno:

```python
        await redis.delete(
            f"arq:job:{job_id}",
            f"arq:in-progress:{job_id}",
            f"arq:retry:{job_id}",
        )
```

con (in entrambe `_enqueue_scrape_with_redis` e `_enqueue_dm_workers_with_redis`):

```python
        # NON cancellare arq:in-progress: non ferma il coroutine in esecuzione
        # e maschera job vivi → duplicati. Solo job/retry pending.
        await redis.delete(f"arq:job:{job_id}", f"arq:retry:{job_id}")
```

- [ ] **Step 2: Stessa correzione in `daily_reset` e `delete_campaign`**

In `task_queue.py` (`daily_reset`) e `api/campaigns.py` (`delete_campaign`, `_enqueue_pregen`, `_enqueue_full_batch`) rimuovere ovunque la riga `f"arq:in-progress:{...}"` dalle `redis.delete(...)`, mantenendo `arq:job:` e `arq:retry:`.

- [ ] **Step 3: Bloccare delete anche per stati scraping**

In `api/campaigns.py` `delete_campaign` sostituire:

```python
    if campaign.status == CampaignStatus.running:
        raise HTTPException(status_code=400, detail="Cannot delete a running campaign. Pause it first.")
```

con:

```python
    if campaign.status in (
        CampaignStatus.running, CampaignStatus.scraping,
        CampaignStatus.scraping_and_running, CampaignStatus.scraping_break,
    ):
        raise HTTPException(status_code=400,
            detail="Metti in pausa la campagna prima di eliminarla (job attivi).")
```

- [ ] **Step 4: Verifica sintassi**

Run (da `backend/`): `./venv/Scripts/python -m compileall app/services/work_enqueue.py app/workers/task_queue.py app/api/campaigns.py`
Expected: nessun `SyntaxError`.

- [ ] **Step 5: Commit** (skip se non-git)

```bash
git add backend/app/services/work_enqueue.py backend/app/workers/task_queue.py backend/app/api/campaigns.py
git commit -m "fix(queue): never delete arq:in-progress; block delete on active scraping"
```

---

## Task 3: F1 — Worker DM short-lived (batch + re-enqueue)

**Obiettivo:** trasformare `run_campaign_worker` da loop infinito (8h) a job che processa un **batch limitato** (es. fino a `SESSION_MAX_MESSAGES` DM o una pausa sessione) e poi si **ri-accoda con `defer`**, liberando lo slot ARQ tra i batch.

**Files:**
- Modify: `backend/app/workers/message_worker.py` (`run_campaign_task`)
- Modify: `backend/app/services/campaign_orchestrator.py` (`run_campaign_worker`)

- [ ] **Step 1: Introdurre un budget di batch nel worker**

In `campaign_orchestrator.py`, in `run_campaign_worker`, aggiungere all'inizio (vicino agli altri contatori):

```python
    BATCH_DM_BUDGET = settings.session_max_messages  # DM massimi per invocazione job
    dm_in_this_invocation = 0
```

Dopo ogni invio riuscito (dove si fa `session_mgr.record_message_sent()`), aggiungere:

```python
                dm_in_this_invocation += 1
```

Sostituire il punto in cui parte la pausa sessione (`if session_mgr.should_break_session():`) in modo che, invece di dormire la pausa dentro il job, il worker **esca e si ri-accodi**:

```python
            if session_mgr.should_break_session() or dm_in_this_invocation >= BATCH_DM_BUDGET:
                await _close_browser()
                from app.utils.timing import session_break_seconds as _bs
                delay = int(_bs())
                emit_event(campaign_id, "session_break",
                           f"Fine batch — worker si ri-accoda tra {delay//60} min")
                from app.services.work_enqueue import reenqueue_one_dm_worker
                await reenqueue_one_dm_worker(campaign_id, account_id, defer_seconds=delay)
                return
```

(Rimuovere il vecchio blocco `take_session_break_interruptible` da questo punto: la pausa diventa il `defer` del job ri-accodato; il job non resta vivo durante la pausa → slot ARQ libero.)

- [ ] **Step 2: Helper di re-enqueue con defer**

In `work_enqueue.py` aggiungere:

```python
async def reenqueue_one_dm_worker(campaign_id: str, account_id: str, defer_seconds: int) -> None:
    import arq
    redis = await arq.create_pool(arq_redis_settings())
    try:
        job_id = f"worker:{campaign_id}:{account_id}"
        await redis.delete(f"arq:job:{job_id}", f"arq:retry:{job_id}")
        await redis.enqueue_job(
            "run_campaign_task", campaign_id, account_id,
            _job_id=job_id, _defer_by=defer_seconds,
        )
    finally:
        await redis.aclose()
```

- [ ] **Step 3: Abbassare `job_timeout`**

In `task_queue.py` `WorkerSettings` sostituire `job_timeout = 3600 * 8` con `job_timeout = 3600`  (1h: un batch non deve mai durare 8h).

- [ ] **Step 4: Test integrazione manuale (documentare esito)**

Avviare Redis + ARQ worker + API. Avviare una campagna con 1 account e `SESSION_MAX_MESSAGES=3`. Verificare nei log: dopo 3 DM `Fine batch — worker si ri-accoda`, job ricompare dopo il defer, slot ARQ libero nel mezzo (eseguire in parallelo un cron `release_stale_locks` e verificare che giri).
Expected: cron eseguono anche con campagne attive (niente starvation).

- [ ] **Step 5: Verifica sintassi**

Run (da `backend/`): `./venv/Scripts/python -m compileall app/services/campaign_orchestrator.py app/services/work_enqueue.py app/workers/task_queue.py`
Expected: nessun `SyntaxError`.

- [ ] **Step 6: Commit** (skip se non-git)

```bash
git add backend/app/services/campaign_orchestrator.py backend/app/services/work_enqueue.py backend/app/workers/task_queue.py
git commit -m "refactor(queue): short-lived DM worker (batch + deferred re-enqueue)"
```

---

## Task 4: F1 — Processo/coda cron dedicato

**Files:**
- Create: `backend/app/workers/cron_worker.py`
- Modify: `backend/app/workers/task_queue.py` (rimuovere i cron dal worker DM)

- [ ] **Step 1: Estrarre i cron in un WorkerSettings separato**

Create `backend/app/workers/cron_worker.py`:

```python
"""Dedicated ARQ worker for cron jobs — isolated from DM worker concurrency.

Run: arq app.workers.cron_worker.CronWorkerSettings
"""
from arq import cron
from app.services.work_enqueue import arq_redis_settings
from app.workers.task_queue import (
    daily_reset, release_stale_locks, check_replies,
    recover_sending, telegram_commands,
)


class CronWorkerSettings:
    functions = []
    cron_jobs = [
        cron(daily_reset, hour=0, minute=5),
        cron(release_stale_locks, minute={0, 15, 30, 45}),
        cron(check_replies, minute={0, 30}),
        cron(recover_sending, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
        cron(telegram_commands, minute=set(range(60))),
    ]
    redis_settings = arq_redis_settings()
    max_jobs = 5
    keep_result = 0
```

- [ ] **Step 2: Rimuovere i cron dal worker DM**

In `task_queue.py` `WorkerSettings`, svuotare `cron_jobs = []` (i cron ora girano solo in `CronWorkerSettings`). Lasciare `functions` invariato.

- [ ] **Step 3: Documentare l'avvio del secondo worker**

In `backend/pyproject.toml` o `start.bat`/`start.sh` aggiungere il comando: `arq app.workers.cron_worker.CronWorkerSettings` come processo separato accanto a `arq app.workers.task_queue.WorkerSettings`. Aggiornare `CLAUDE.md` sezione "Avvio locale" con il 5° terminale (cron worker).

- [ ] **Step 4: Verifica sintassi**

Run (da `backend/`): `./venv/Scripts/python -m compileall app/workers/cron_worker.py app/workers/task_queue.py`
Expected: nessun `SyntaxError`.

- [ ] **Step 5: Commit** (skip se non-git)

```bash
git add backend/app/workers/cron_worker.py backend/app/workers/task_queue.py CLAUDE.md
git commit -m "refactor(queue): dedicated cron worker (no starvation by DM jobs)"
```

---

## Task 5: M8 — Migrazioni fuori dal boot API

**Files:**
- Create: `backend/scripts/migrate.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Script di migrazione standalone**

Create `backend/scripts/migrate.py`:

```python
"""Run Alembic migrations to head. Use as deploy step, not at API boot.

Usage from backend/: python -m scripts.migrate
"""
import sys
from pathlib import Path

sys.path.insert(0, ".")


def main() -> int:
    from alembic.config import Config
    from alembic import command
    from app.config import settings
    from app.utils.db_dialect import to_async_database_url
    ini = Path(__file__).parent.parent / "alembic.ini"
    cfg = Config(str(ini))
    db_url = to_async_database_url(settings.database_url).replace("%", "%%")
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")
    print("Migrations applied to head.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Rimuovere la migrazione dal lifespan**

In `backend/app/main.py`, nel `lifespan`, rimuovere la riga `await _run_migrations()` e la funzione `_run_migrations`. Aggiungere un log all'avvio:

```python
    logger.info("Migrazioni NON eseguite al boot — lanciare 'python -m scripts.migrate' nel deploy")
```

- [ ] **Step 3: Aggiornare start.bat / start.sh / CLAUDE.md**

Aggiungere `python -m scripts.migrate` come passo prima di avviare uvicorn negli script di avvio e nella sezione "Avvio locale" di `CLAUDE.md`.

- [ ] **Step 4: Verifica**

Run (da `backend/`): `./venv/Scripts/python -m scripts.migrate`
Expected: `Migrations applied to head.`

- [ ] **Step 5: Commit** (skip se non-git)

```bash
git add backend/scripts/migrate.py backend/app/main.py start.bat start.sh CLAUDE.md
git commit -m "refactor(db): move Alembic upgrade out of API boot into deploy step"
```

---

## Task 6: Reservation table + state machine DM (strategico)

**Obiettivo:** separare "contatto già lavorato" (`global_contacts`) da "prenotazione temporanea" (nuova `contact_reservations` con owner job + expiry). State machine DM: `pending → reserved → sending → sent | retry | failed | unknown`.

**Files:**
- Create: `backend/alembic/versions/011_contact_reservations.py`
- Create: `backend/app/models/contact_reservation.py`
- Modify: `backend/app/services/campaign_orchestrator.py` (reserve/release usano la nuova tabella)
- Create: `backend/app/services/reservation.py` (logica centralizzata)
- Test: `backend/tests/test_reservation.py`

- [ ] **Step 1: Migrazione tabella**

Create `backend/alembic/versions/011_contact_reservations.py`:

```python
"""contact_reservations: temporary per-job reservation with expiry.

Revision ID: 011
Revises: 010
"""
from alembic import op
import sqlalchemy as sa

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "contact_reservations",
        sa.Column("ig_user_id", sa.BigInteger, primary_key=True),
        sa.Column("owner_job", sa.String(128), nullable=False),
        sa.Column("campaign_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("expires_at", sa.DateTime, nullable=False),
    )
    op.create_index("idx_reservations_expiry", "contact_reservations", ["expires_at"])


def downgrade() -> None:
    op.drop_table("contact_reservations")
```

- [ ] **Step 2: Modello**

Create `backend/app/models/contact_reservation.py`:

```python
from datetime import datetime
from sqlalchemy import String, BigInteger, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class ContactReservation(Base):
    __tablename__ = "contact_reservations"

    ig_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_job: Mapped[str] = mapped_column(String(128), nullable=False)
    campaign_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
```

- [ ] **Step 3: Servizio reservation centralizzato**

Create `backend/app/services/reservation.py`:

```python
"""Temporary contact reservation (separata da global_contacts = contatto lavorato)."""
import uuid
from datetime import datetime, timedelta
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.contact_reservation import ContactReservation
from app.utils.db_dialect import upsert_ignore
from app.config import settings

RESERVATION_TTL_MINUTES = 30


async def try_reserve(ig_user_id: int, owner_job: str, campaign_id: str, db: AsyncSession) -> bool:
    now = datetime.utcnow()
    # Pulisci prenotazioni scadute (qualunque job morto)
    await db.execute(delete(ContactReservation).where(ContactReservation.expires_at < now))
    stmt = upsert_ignore(
        ContactReservation,
        {
            "ig_user_id": ig_user_id,
            "owner_job": owner_job,
            "campaign_id": campaign_id,
            "created_at": now,
            "expires_at": now + timedelta(minutes=RESERVATION_TTL_MINUTES),
        },
        "ig_user_id",
        settings.database_url,
    )
    res = await db.execute(stmt)
    await db.commit()
    return res.rowcount == 1


async def release(ig_user_id: int, db: AsyncSession) -> None:
    await db.execute(delete(ContactReservation).where(ContactReservation.ig_user_id == ig_user_id))
```

- [ ] **Step 4: Sostituire i 3 helper nell'orchestrator**

In `campaign_orchestrator.py`, sostituire le chiamate a `_try_reserve_global_contact` con `reservation.try_reserve(follower.ig_user_id, f"worker:{campaign_id}:{account_id}", campaign_id, db)` e `_release_global_contact_reservation` con `reservation.release(follower.ig_user_id, db)` (import `from app.services import reservation`). `_mark_globally_contacted` resta su `global_contacts` (= contatto lavorato, definitivo, su invio riuscito) e in più: dopo il mark, chiamare `await reservation.release(...)` (la prenotazione temporanea non serve più). In `recovery_checker.py` aggiornare l'import/chiamata di rilascio a `reservation.release`.

- [ ] **Step 5: Test**

Create `backend/tests/test_reservation.py`:

```python
import pytest
from datetime import datetime
from app.services import reservation
from app.database import AsyncSessionLocal


@pytest.mark.asyncio
async def test_reserve_is_exclusive_then_releasable():
    ig = 880000000000 + int(datetime.utcnow().timestamp()) % 100000
    async with AsyncSessionLocal() as db:
        assert await reservation.try_reserve(ig, "jobA", "c1", db) is True
        assert await reservation.try_reserve(ig, "jobB", "c1", db) is False
        await reservation.release(ig, db)
        await db.commit()
        assert await reservation.try_reserve(ig, "jobB", "c1", db) is True
        await reservation.release(ig, db)
        await db.commit()
```

- [ ] **Step 6: Migrazione + test + sintassi**

Run (da `backend/`): `./venv/Scripts/python -m scripts.migrate && ./venv/Scripts/python -m pytest tests/test_reservation.py -v && ./venv/Scripts/python -m compileall app/services/reservation.py app/models/contact_reservation.py app/services/campaign_orchestrator.py app/services/recovery_checker.py`
Expected: migrazione 010→011, 1 passed, no `SyntaxError`.

- [ ] **Step 7: Commit** (skip se non-git)

```bash
git add backend/alembic/versions/011_contact_reservations.py backend/app/models/contact_reservation.py backend/app/services/reservation.py backend/app/services/campaign_orchestrator.py backend/app/services/recovery_checker.py backend/tests/test_reservation.py
git commit -m "refactor(dm): separate temporary reservations from worked contacts"
```

---

## Task 7: Account lease + heartbeat (F2)

**Files:**
- Create: `backend/app/services/account_lease.py`
- Modify: `backend/app/services/campaign_orchestrator.py`

- [ ] **Step 1: Lease su `instagram_accounts` via colonna leggera**

Riusare la tabella esistente: aggiungere via migrazione 012 due colonne `lease_owner` (String) e `lease_expires_at` (DateTime) su `instagram_accounts`. Create `backend/alembic/versions/012_account_lease.py` (stesso schema delle migrazioni precedenti, `down_revision="011"`, `op.add_column` per le due colonne nullable).

- [ ] **Step 2: Servizio lease**

Create `backend/app/services/account_lease.py` con `acquire(account_id, owner, db, ttl_min=15) -> bool` (UPDATE ... WHERE lease_owner IS NULL OR lease_expires_at < now), `heartbeat(account_id, owner, db)` (estende expires_at), `release(account_id, owner, db)`. Codice analogo al pattern di `reservation.py` ma con `update().where(...)` e check `rowcount==1`.

- [ ] **Step 3: Integrare nel worker DM short-lived**

All'inizio di `run_campaign_worker`, dopo i check stato: `if not await account_lease.acquire(account_id, job_id, db): return` (un altro job ha l'account). Heartbeat ogni N DM. `release` nel `finally`.

- [ ] **Step 4: Migrazione + sintassi + test integrazione**

Run (da `backend/`): `./venv/Scripts/python -m scripts.migrate && ./venv/Scripts/python -m compileall app/services/account_lease.py app/services/campaign_orchestrator.py`
Test manuale: due job stesso account → solo uno procede, l'altro esce.

- [ ] **Step 5: Commit** (skip se non-git)

```bash
git add backend/alembic/versions/012_account_lease.py backend/app/services/account_lease.py backend/app/services/campaign_orchestrator.py
git commit -m "feat(queue): DB account lease + heartbeat (cooperative, crash-safe)"
```

---

## Task 8: Adapter modulari Instagram/AI/browser

**Files:**
- Create: `backend/app/adapters/__init__.py`, `backend/app/adapters/ai.py`, `backend/app/adapters/instagram.py`, `backend/app/adapters/browser.py`

- [ ] **Step 1: Definire le interfacce (Protocol)**

Create `backend/app/adapters/ai.py`:

```python
from typing import Protocol


class AIClient(Protocol):
    async def generate(self, system_prompt: str, user_prompt: str, max_tokens: int) -> str: ...
```

`backend/app/adapters/instagram.py` (Protocol `IGClient` con `scrape_followers_chunk`, `user_info`, `direct_threads`, `direct_pending_inbox`), `backend/app/adapters/browser.py` (Protocol `DMBrowser` con `open`, `ensure_logged_in`, `browse_feed`, `send_dm`, `close`).

- [ ] **Step 2: Wrappare le implementazioni esistenti dietro le interfacce**

`ai_personalizer` espone una factory `get_ai_client() -> AIClient` che ritorna l'impl per provider; `BrowserSession`/`InstagramPage` dichiarano (via duck typing) di soddisfare `DMBrowser`. Nessun cambio di comportamento — solo punto di iniezione per i test.

- [ ] **Step 3: Test con fake adapter**

Create `backend/tests/test_orchestrator_with_fakes.py`: un `FakeDMBrowser` che registra `send_dm` chiamato; un `FakeAIClient` che ritorna testo fisso. Verifica che un follower `bio_scraped` con `require_approval=False` produca uno `send_dm` e follower `sent`, senza servizi reali. (Richiede refactor minimo per iniettare le factory — definirlo qui come parte del Task.)

- [ ] **Step 4: Sintassi + test**

Run (da `backend/`): `./venv/Scripts/python -m pytest tests/test_orchestrator_with_fakes.py -v`
Expected: passed.

- [ ] **Step 5: Commit** (skip se non-git)

```bash
git add backend/app/adapters/ backend/tests/test_orchestrator_with_fakes.py backend/app/services/ai_personalizer.py
git commit -m "refactor: adapter interfaces for AI/IG/browser (testable orchestration)"
```

---

## Task 9: Osservabilità operativa

**Files:**
- Create: `backend/app/api/ops.py`
- Modify: `backend/app/main.py` (registrare router, gated `require_admin`)
- Frontend: nuova pagina `frontend/app/ops/page.tsx` (lista)

- [ ] **Step 1: Endpoint diagnostico**

Create `backend/app/api/ops.py` con `GET /ops/summary` (admin) che ritorna: messaggi `sending` più vecchi di 10 min (count + lista), `contact_reservations` scadute, follower lockati >20 min, campagne `running` con `updated_at` >30 min, account per status. Query read-only.

- [ ] **Step 2: Registrare router gated**

In `main.py`: `from app.api import ops` e `app.include_router(ops.router, prefix="/api", dependencies=_protected)` (ops usa `require_admin` internamente).

- [ ] **Step 3: Pagina frontend minimale**

Create `frontend/app/ops/page.tsx` che fa `useSWR` su `/ops/summary` (refresh 30s) e mostra tabelle. Aggiungere voce in Sidebar.

- [ ] **Step 4: Sintassi**

Run (da `backend/`): `./venv/Scripts/python -m compileall app/api/ops.py app/main.py`
Expected: nessun `SyntaxError`.

- [ ] **Step 5: Commit** (skip se non-git)

```bash
git add backend/app/api/ops.py backend/app/main.py frontend/app/ops/page.tsx frontend/components/layout/Sidebar.tsx
git commit -m "feat(ops): operational observability endpoint + page"
```

---

## Task 10: M4 — Claim follower senza full-scan random

**Files:**
- Modify: `backend/app/services/campaign_orchestrator.py` (`_claim_next_follower`)

- [ ] **Step 1: Campionamento via OFFSET su conteggio**

Sostituire la SELECT con `.order_by(func.random()).limit(1)` con: contare i candidati `n` (query COUNT già indicizzata da `idx_followers_claim`), se `n==0` return None, scegliere `off = random.randint(0, min(n, 500) - 1)` e `.offset(off).limit(1)` senza `order_by(random())`. Mantiene la non-sequenzialità senza ordinare l'intero set.

```python
        base = (
            select(Follower)
            .where(
                Follower.campaign_id == campaign_id,
                Follower.status.in_([FollowerStatus.bio_scraped, FollowerStatus.message_generated]),
                Follower.locked_by_account_id.is_(None),
            )
        )
        n = await db.scalar(select(func.count()).select_from(base.subquery()))
        if not n:
            return None
        import random as _r
        off = _r.randint(0, min(n, 500) - 1)
        result = await db.execute(base.offset(off).limit(1))
```

- [ ] **Step 2: Sintassi + test esistenti**

Run (da `backend/`): `./venv/Scripts/python -m compileall app/services/campaign_orchestrator.py && ./venv/Scripts/python -m pytest tests/ -v`
Expected: no `SyntaxError`, test verdi.

- [ ] **Step 3: Commit** (skip se non-git)

```bash
git add backend/app/services/campaign_orchestrator.py
git commit -m "perf(claim): sample by offset instead of ORDER BY random() full-scan"
```

---

## Task 11: Ambiente riproducibile (lockfile, pin, font offline, CI)

**Files:**
- Modify: `backend/requirements.txt` (pin versioni), `backend/pyproject.toml`
- Modify: `frontend` font config
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Pin versioni backend**

In `requirements.txt` sostituire i `>=` con versioni esatte correnti (ricavarle: `./venv/Scripts/python -m pip freeze`). Allineare `pyproject.toml` `dependencies` allo stesso set + `optional-dependencies.dev = ["pytest","pytest-asyncio","anyio"]`.

- [ ] **Step 2: Font self-hosted (build offline)**

Sostituire l'uso di `next/font/google` nel layout con un font locale (`next/font/local`) o fallback di sistema. Verificare `npm run build` senza rete.

- [ ] **Step 3: CI minimale**

Create `.github/workflows/ci.yml` con job: setup python, `pip install -r backend/requirements.txt`, `python -m compileall backend/app`, `pytest backend/tests`; job node: `npm ci`, `npm run lint`, `npm run build`.

- [ ] **Step 4: Verifica**

Run: `npm run build` (frontend, offline) → OK. `./venv/Scripts/python -m pytest tests/ -v` → verde.

- [ ] **Step 5: Commit** (skip se non-git)

```bash
git add backend/requirements.txt backend/pyproject.toml frontend .github/workflows/ci.yml
git commit -m "chore: pinned deps, offline font, CI pipeline"
```

---

## Self-Review

- **Spec coverage:** C3/C4/U5 follow-through (T1,T2,T3,T4), M8 (T5), reservation+state machine strategico (T6), lease/heartbeat F2 (T7), adapter modulari (T8), osservabilità (T9), M4 (T10), B-series ambiente/CI (T11). Le funzionalità di prodotto (lead scoring, suppression list, inbox unificata, warm-up comportamentale, proxy health) restano nel backlog di `AUDIT_UNIFICATO.md` §6 — fuori scope Fase 2 (infrastruttura prima del prodotto).
- **Placeholder scan:** i Task 7/8/9 contengono passi con codice descritto a livello di firma/Protocol invece di corpo completo: è una scelta consapevole per refactor architetturali dove il corpo dipende dall'integrazione del Task precedente; ogni Task ha comunque verifica e criterio di accettazione concreti. Eseguire con sub-skill subagent-driven (review fra Task).
- **Type consistency:** `reservation.try_reserve/release` (T6) usato coerente in orchestrator e recovery; `account_lease.acquire/heartbeat/release` (T7) firma stabile; `reenqueue_one_dm_worker` (T3) definito in work_enqueue e chiamato solo lì.

---

## Execution Handoff

Fase 2 = refactor: usare **subagent-driven-development**, un Task per subagent, review fra Task. Ordine obbligatorio: T1 → T2 → T3 → T4 → T5 (queue/infra) prima di T6/T7 (stato/lease), poi T8/T9/T10/T11. Non parallelizzare T3 e T6 (toccano entrambi l'orchestrator).
