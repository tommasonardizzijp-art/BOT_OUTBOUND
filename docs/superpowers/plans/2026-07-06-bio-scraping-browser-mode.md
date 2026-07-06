# Fase Bio via Browser (`bio_engine=browser`) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Aggiungere alla Fase Bio un motore alternativo `browser` (Patchright) selezionabile per campagna, che apre ogni profilo in un browser reale e cattura `web_profile_info` invece di usare l'API instagrapi — multi-account in parallelo (1 task ARQ per account), pool disgiunti via lock atomico, timing umano.

**Architecture:** `Campaign.bio_engine` biforca `scrape_bios`: se `browser`, fa fan-out di N task ARQ (uno per account scraping) con stagger via `_defer_by`. Ogni task esegue una **mini-sessione** browser (apre, scrapa fino a un cap di profili, chiude) e poi fa `Retry(defer)` per la pausa lunga — job corti, mai oltre `job_timeout=3600s`. L'estrazione riusa `fetch_and_store_bio_browser` già esistente (nessun DOM scraping). Il path API resta intatto.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.x async, ARQ (Redis), Alembic, Patchright, pytest + pytest-asyncio. Frontend Next.js 14 + shadcn/ui.

## Global Constraints

- Il path API di `scrape_bios` (ScrapingPool, gestione `capped`/`challenge`/`soft_block`, micro-yield) **non va toccato**. La biforcazione è additiva.
- Ogni task ARQ apre la **propria** `AsyncSessionLocal()`: le sessioni SQLAlchemy async non sono concorrenti-safe.
- Ogni job ARQ deve restare **corto** (mini-sessione ≪ `job_timeout=3600s`). Pausa lunga = `Retry(defer)`, mai `sleep` prolungato in-job.
- Lock follower: il campo `locked_by_account_id`/`locked_at` è condiviso con la fase DM ma opera su status disgiunti (bio=`pending`, DM=`bio_scraped`/`message_generated`). Su outcome `done`/`skipped` il lock **va rilasciato** (`= None`) nello stesso commit del cambio status (C2 dello spec).
- `LOCK_TIMEOUT_MINUTES = 20` (da `app.services.campaign_orchestrator`). Riusare, non ridefinire.
- Migration = Alembic (`backend/alembic/versions/`), applicata con `python -m scripts.migrate`. Non usare `create_all`/ALTER inline.
- Test DB async: `async with AsyncSessionLocal() as db`, `@pytest.mark.asyncio`, ID/username unici per evitare collisioni sul DB dev condiviso (pattern `tests/test_reservation.py`).
- Comando test: da `backend/`, `venv\Scripts\activate` poi `pytest tests/<file> -v`.
- Riferimento di verità: `docs/superpowers/specs/2026-07-06-bio-scraping-browser-mode-design.md`.

---

## File Structure

| File | Responsabilità | Azione |
|---|---|---|
| `backend/app/models/campaign.py` | campo `bio_engine` | Modify |
| `backend/alembic/versions/021_bio_engine.py` | migration additiva colonna | Create |
| `backend/app/schemas/campaign.py` | `bio_engine` in Create/Update/Response | Modify |
| `backend/app/config.py` | config `bio_browser_*` | Modify |
| `backend/app/services/browser_bio.py` | `claim_next_pending`, `maybe_micro_scroll`, `scrape_bios_browser_session`, `enqueue_browser_bio_workers`, `browser_bio_job_id`; rilascio lock in `fetch_and_store_bio_browser` | Modify |
| `backend/app/workers/task_queue.py` | task `browser_bio_account_task` + registrazione in `WorkerSettings.functions` | Modify |
| `backend/app/services/scrape_bios.py` | biforcazione in cima + gate `run_pause_browser_all_accounts` | Modify |
| `frontend/` (form campagna) | dropdown "Motore Fase Bio" | Modify |
| `backend/tests/test_bio_engine_*.py`, `test_claim_next_pending.py`, `test_micro_scroll.py`, `test_browser_bio_enqueue.py`, `test_scrape_bios_browser_fork.py` | test | Create |
| `docs/project/PROGRESS.md`, `INDEX.md`, `CLAUDE.md` | contesto | Modify (Task finale) |

---

## Task 1: Campo `bio_engine` (modello + migration + schema)

**Files:**
- Modify: `backend/app/models/campaign.py`
- Create: `backend/alembic/versions/021_bio_engine.py`
- Modify: `backend/app/schemas/campaign.py`
- Test: `backend/tests/test_bio_engine_column.py`

**Interfaces:**
- Produces: `Campaign.bio_engine: str` (default `'api'`, `nullable=False`). Valori validi: `'api'` | `'browser'`.

- [ ] **Step 1: Write the failing test**

`backend/tests/test_bio_engine_column.py`:
```python
"""Fase Bio: colonna bio_engine sul modello Campaign."""
from app.models.campaign import Campaign


def test_bio_engine_column_present():
    assert "bio_engine" in Campaign.__table__.columns.keys()


def test_bio_engine_default_api():
    col = Campaign.__table__.columns["bio_engine"]
    assert col.default.arg == "api"
    assert col.nullable is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_bio_engine_column.py -v`
Expected: FAIL — `KeyError: 'bio_engine'` / assert su colonna assente.

- [ ] **Step 3: Add the column to the model**

In `backend/app/models/campaign.py`, subito dopo il campo `inbox_engine` (~riga 72), aggiungi:
```python
    # Motore Fase Bio. 'api' = instagrapi (user_info, veloce, consuma cap). Default.
    # 'browser' = Patchright (web_profile_info, prudente, no cap API). Vedi migration 021.
    bio_engine: Mapped[str] = mapped_column(
        String(10), nullable=False, default='api', server_default='api'
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_bio_engine_column.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Create the Alembic migration**

La head corrente è `021_current_session_cap` (verifica con `ls alembic/versions/ | tail`). Crea `backend/alembic/versions/022_bio_engine.py`:
```python
"""Fase Bio: colonna bio_engine su campaigns.

Revision ID: 022
Revises: 021
Create Date: 2026-07-06
"""
from alembic import op
import sqlalchemy as sa

revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column("bio_engine", sa.String(length=10), nullable=False, server_default="api"),
    )


def downgrade() -> None:
    op.drop_column("campaigns", "bio_engine")
```
Se `revision` di `021_current_session_cap.py` non è la stringa `"021"`, imposta `down_revision` al valore reale letto da quel file.

- [ ] **Step 6: Apply the migration**

Run: `python -m scripts.migrate`
Expected: `Migrations applied to head.` senza errori.

- [ ] **Step 7: Add `bio_engine` to schemas**

In `backend/app/schemas/campaign.py`, replica esattamente come è fatto `inbox_engine` (stessa presenza in Create/Update/Response). Tipicamente:
- `CampaignCreate`: `bio_engine: str = "api"`
- `CampaignUpdate`: `bio_engine: str | None = None`
- `CampaignResponse`: `bio_engine: str`

Verifica prima con: `grep -n "inbox_engine" backend/app/schemas/campaign.py` e aggiungi `bio_engine` accanto a ogni occorrenza, stesso stile.

- [ ] **Step 8: Verify schema import + full test**

Run: `pytest tests/test_bio_engine_column.py -v && python -c "from app.schemas.campaign import CampaignCreate, CampaignResponse; print('ok')"`
Expected: test PASS, stampa `ok`.

- [ ] **Step 9: Commit**

```bash
git add backend/app/models/campaign.py backend/alembic/versions/022_bio_engine.py backend/app/schemas/campaign.py backend/tests/test_bio_engine_column.py
git commit -m "feat(bio): campo bio_engine su Campaign + migration 022 + schema"
```

---

## Task 2: Config `bio_browser_*`

**Files:**
- Modify: `backend/app/config.py`
- Test: `backend/tests/test_bio_browser_settings.py`

**Interfaces:**
- Produces: `settings.bio_browser_headless: bool`, `settings.bio_browser_scroll_ratio: float`, `settings.bio_browser_scroll_min_s: float`, `settings.bio_browser_scroll_max_s: float`, `settings.bio_browser_daily_limit: int | None`, `settings.bio_browser_stagger_min_s: float`, `settings.bio_browser_stagger_max_s: float`, `settings.bio_browser_session_cap_min: int`, `settings.bio_browser_session_cap_max: int`.

**Perché un cap dedicato:** il cap del path API `bio_session_cap_min/max` = 150-300 è tarato sull'API veloce (~3s/profilo → ~450s). Col browser (~15s/profilo) 150-300 profili = 37-75 min per mini-sessione, **oltre `job_timeout=3600s`**. Il cap browser va tenuto piccolo (20-40 profili → 5-10 min/mini-sessione, largo margine).

- [ ] **Step 1: Write the failing test**

`backend/tests/test_bio_browser_settings.py`:
```python
"""Config del motore bio browser: default sicuri per il test."""
from app.config import settings


def test_bio_browser_defaults():
    assert settings.bio_browser_headless is False           # test: visibile
    assert 0.0 <= settings.bio_browser_scroll_ratio <= 1.0
    assert settings.bio_browser_scroll_min_s <= settings.bio_browser_scroll_max_s
    assert settings.bio_browser_daily_limit is None          # nessun cap di default
    assert settings.bio_browser_stagger_min_s <= settings.bio_browser_stagger_max_s


def test_bio_browser_session_cap_fits_job_timeout():
    # cap * ~15s/profilo deve stare ben sotto job_timeout=3600s
    assert settings.bio_browser_session_cap_min <= settings.bio_browser_session_cap_max
    assert settings.bio_browser_session_cap_max * 15 < 3600
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_bio_browser_settings.py -v`
Expected: FAIL — `AttributeError: ... bio_browser_headless`.

- [ ] **Step 3: Add settings**

In `backend/app/config.py`, vicino ai `bio_browser_batch_*` esistenti (~riga 142), aggiungi:
```python
    # --- Motore Fase Bio via browser (bio_engine='browser') ---
    bio_browser_headless: bool = False          # test: finestra visibile; prod: True
    bio_browser_scroll_ratio: float = 0.35      # frazione profili con micro-scroll
    bio_browser_scroll_min_s: float = 4.0
    bio_browser_scroll_max_s: float = 5.0
    bio_browser_daily_limit: int | None = None  # cap opzionale profili/account/giorno (None = off)
    bio_browser_stagger_min_s: float = 60.0     # differita prima apertura per account
    bio_browser_stagger_max_s: float = 180.0
    # Cap profili per mini-sessione: PICCOLO (browser ~15s/profilo → deve stare
    # sotto job_timeout=3600s). Distinto da bio_session_cap_min/max (path API).
    bio_browser_session_cap_min: int = 20
    bio_browser_session_cap_max: int = 40
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_bio_browser_settings.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/config.py backend/tests/test_bio_browser_settings.py
git commit -m "feat(bio): config bio_browser_* (headless, scroll, stagger, cap)"
```

---

## Task 3: Rilascio lock in `fetch_and_store_bio_browser` (C2)

**Files:**
- Modify: `backend/app/services/browser_bio.py:184-216` (blocco scrittura Follower in `fetch_and_store_bio_browser`)
- Test: `backend/tests/test_browser_bio_lock_release.py`

**Interfaces:**
- Consumes: `Follower.locked_by_account_id`, `Follower.locked_at` (esistenti).
- Produces: dopo un outcome `done`, il Follower ha `status == bio_scraped` **e** `locked_by_account_id is None` e `locked_at is None`.

**Contesto:** oggi `fetch_and_store_bio_browser` non azzera il lock perché il claim atomico non esisteva. Con Task 4 il follower arriva qui **già lockato**; se non lo liberiamo, resta invisibile alla fase DM per 20 min.

- [ ] **Step 1: Write the failing test**

`backend/tests/test_browser_bio_lock_release.py`:
```python
"""C2: dopo scrape bio via browser, il lock del claim va rilasciato."""
from datetime import datetime
import pytest

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.services import browser_bio


class _FakePage:
    def __init__(self, user): self._user = user
    async def _get_page(self): return self  # non usato: patchiamo _capture_web_profile_info


class _FakeSession:
    def __init__(self, user): self.page = _FakePage(user)


@pytest.mark.asyncio
async def test_lock_released_on_done(monkeypatch):
    uid = 990000000000 + int(datetime.utcnow().timestamp()) % 100000
    async with AsyncSessionLocal() as db:
        camp = Campaign(name="t", status=CampaignStatus.scraping, source_type="scrape")
        db.add(camp); await db.flush()
        f = Follower(
            campaign_id=camp.id, ig_user_id=uid, username=f"u{uid}",
            status=FollowerStatus.pending,
            locked_by_account_id="acc-1", locked_at=datetime.utcnow(),
        )
        db.add(f); await db.commit(); await db.refresh(f)

        async def fake_capture(raw_page, username, timeout_s=8.0):
            return {"id": str(uid), "username": username, "full_name": "X",
                    "biography": "bio", "edge_followed_by": {"count": 1},
                    "edge_follow": {"count": 1}}
        monkeypatch.setattr(browser_bio, "_capture_web_profile_info", fake_capture)

        outcome, err = await browser_bio.fetch_and_store_bio_browser(f, camp, db, _FakeSession({}))
        assert outcome == "done"
        await db.refresh(f)
        assert f.status == FollowerStatus.bio_scraped
        assert f.locked_by_account_id is None
        assert f.locked_at is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_browser_bio_lock_release.py -v`
Expected: FAIL — `assert f.locked_by_account_id is None` fallisce (resta `"acc-1"`).

- [ ] **Step 3: Release the lock in the `done` write block**

In `backend/app/services/browser_bio.py`, nel blocco di `fetch_and_store_bio_browser` che imposta `follower.status = FollowerStatus.bio_scraped` (~riga 201), aggiungi subito prima di `await db.commit()`:
```python
    follower.status = FollowerStatus.bio_scraped
    follower.locked_by_account_id = None   # C2: libera il claim atomico (Task 4)
    follower.locked_at = None
    await db.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_browser_bio_lock_release.py -v`
Expected: PASS.

- [ ] **Step 5: Verify no regression on mapping test**

Run: `pytest tests/test_browser_bio_mapping.py -v`
Expected: PASS (invariato).

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/browser_bio.py backend/tests/test_browser_bio_lock_release.py
git commit -m "fix(bio): rilascia lock follower al passaggio a bio_scraped (C2)"
```

---

## Task 4: `claim_next_pending` — claim atomico con stale release

**Files:**
- Modify: `backend/app/services/browser_bio.py` (nuova funzione)
- Test: `backend/tests/test_claim_next_pending.py`

**Interfaces:**
- Produces: `async def claim_next_pending(db, campaign_id: str, account_id: str) -> Follower | None` — claima atomicamente un Follower `pending` non lockato (settando `locked_by_account_id`, `locked_at`), rilasciando prima gli stale lock della campagna. Ritorna il Follower claimato o `None` se non ce ne sono.

- [ ] **Step 1: Write the failing test**

`backend/tests/test_claim_next_pending.py`:
```python
"""Claim atomico dei pending: pool disgiunti tra account + stale release."""
from datetime import datetime, timedelta
import pytest

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.services.browser_bio import claim_next_pending


async def _mk_campaign_with_pending(db, n):
    camp = Campaign(name="t", status=CampaignStatus.scraping, source_type="scrape")
    db.add(camp); await db.flush()
    base = 970000000000 + int(datetime.utcnow().timestamp()) % 100000
    for i in range(n):
        db.add(Follower(campaign_id=camp.id, ig_user_id=base + i,
                        username=f"u{base+i}", status=FollowerStatus.pending))
    await db.commit()
    return camp


@pytest.mark.asyncio
async def test_two_accounts_get_disjoint_followers():
    async with AsyncSessionLocal() as db:
        camp = await _mk_campaign_with_pending(db, 2)
        a = await claim_next_pending(db, camp.id, "acc-A")
        b = await claim_next_pending(db, camp.id, "acc-B")
        assert a is not None and b is not None
        assert a.id != b.id                      # pool disgiunti
        assert a.locked_by_account_id == "acc-A"
        assert b.locked_by_account_id == "acc-B"
        # esauriti: terzo claim None
        assert await claim_next_pending(db, camp.id, "acc-A") is None


@pytest.mark.asyncio
async def test_stale_lock_is_reclaimed():
    async with AsyncSessionLocal() as db:
        camp = await _mk_campaign_with_pending(db, 1)
        f = (await claim_next_pending(db, camp.id, "acc-dead"))
        assert f is not None
        # simula sessione morta: lock vecchio > 20 min, ancora pending
        f.locked_at = datetime.utcnow() - timedelta(minutes=25)
        await db.commit()
        again = await claim_next_pending(db, camp.id, "acc-live")
        assert again is not None and again.id == f.id
        assert again.locked_by_account_id == "acc-live"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_claim_next_pending.py -v`
Expected: FAIL — `ImportError: cannot import name 'claim_next_pending'`.

- [ ] **Step 3: Implement `claim_next_pending`**

In `backend/app/services/browser_bio.py`, aggiungi (import in cima al file se mancano: `from datetime import timedelta`, `from sqlalchemy import update`):
```python
async def claim_next_pending(db, campaign_id: str, account_id: str):
    """Claima atomicamente un Follower pending non lockato per questo account.
    Rilascia prima gli stale lock della campagna (sessioni morte). Ritorna il
    Follower claimato o None. Optimistic lock: safe con più account paralleli
    (SQLite WAL / Postgres). Stesso schema del claim DM in campaign_orchestrator.
    """
    from sqlalchemy import select
    from app.models.follower import Follower, FollowerStatus
    from app.services.campaign_orchestrator import LOCK_TIMEOUT_MINUTES

    stale_cutoff = datetime.utcnow() - timedelta(minutes=LOCK_TIMEOUT_MINUTES)
    await db.execute(
        update(Follower).where(
            Follower.campaign_id == campaign_id,
            Follower.status == FollowerStatus.pending,
            Follower.locked_by_account_id.isnot(None),
            Follower.locked_at < stale_cutoff,
        ).values(locked_by_account_id=None, locked_at=None)
    )
    await db.commit()

    for _ in range(25):  # ritenta se un altro account claima tra SELECT e UPDATE
        follower = (await db.execute(
            select(Follower).where(
                Follower.campaign_id == campaign_id,
                Follower.status == FollowerStatus.pending,
                Follower.locked_by_account_id.is_(None),
            ).limit(1)
        )).scalar_one_or_none()
        if follower is None:
            return None
        claim = await db.execute(
            update(Follower).where(
                Follower.id == follower.id,
                Follower.locked_by_account_id.is_(None),
            ).values(locked_by_account_id=account_id, locked_at=datetime.utcnow())
        )
        await db.commit()
        if claim.rowcount == 1:
            await db.refresh(follower)
            return follower
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_claim_next_pending.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/browser_bio.py backend/tests/test_claim_next_pending.py
git commit -m "feat(bio): claim_next_pending atomico con stale release (pool disgiunti)"
```

---

## Task 5: `maybe_micro_scroll`

**Files:**
- Modify: `backend/app/services/browser_bio.py` (nuova funzione)
- Test: `backend/tests/test_micro_scroll.py`

**Interfaces:**
- Produces: `async def maybe_micro_scroll(session, *, rng=None) -> bool` — con probabilità `settings.bio_browser_scroll_ratio` fa uno scroll leggero di `bio_browser_scroll_min_s`–`max_s` secondi sulla pagina già aperta. Ritorna `True` se ha scrollato, `False` altrimenti. `rng` iniettabile per test (default `random`).

- [ ] **Step 1: Write the failing test**

`backend/tests/test_micro_scroll.py`:
```python
"""Micro-scroll: probabilistico e non-bloccante; scroll solo entro il ratio."""
import random
import pytest

from app.services import browser_bio


class _RawPage:
    def __init__(self): self.scrolled = 0
    async def evaluate(self, *a, **k): self.scrolled += 1


class _Page:
    def __init__(self, raw): self._raw = raw
    async def _get_page(self): return self._raw


class _Session:
    def __init__(self): self.page = _Page(_RawPage())


@pytest.mark.asyncio
async def test_scrolls_when_below_ratio(monkeypatch):
    monkeypatch.setattr(browser_bio.settings, "bio_browser_scroll_ratio", 1.0)
    monkeypatch.setattr(browser_bio.asyncio, "sleep", lambda *_: _noop())
    s = _Session()
    did = await browser_bio.maybe_micro_scroll(s, rng=random.Random(1))
    assert did is True


@pytest.mark.asyncio
async def test_skips_when_ratio_zero(monkeypatch):
    monkeypatch.setattr(browser_bio.settings, "bio_browser_scroll_ratio", 0.0)
    s = _Session()
    did = await browser_bio.maybe_micro_scroll(s, rng=random.Random(1))
    assert did is False


async def _noop():
    return None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_micro_scroll.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'maybe_micro_scroll'`.

- [ ] **Step 3: Implement `maybe_micro_scroll`**

In `backend/app/services/browser_bio.py`:
```python
async def maybe_micro_scroll(session, *, rng=None) -> bool:
    """Scroll leggero sul profilo aperto, ~bio_browser_scroll_ratio dei profili,
    per 4-5s. Simula lo sguardo umano; non su tutti (la costanza è una firma).
    Difensivo: non solleva. Ritorna True se ha scrollato."""
    r = rng or random
    if r.random() >= settings.bio_browser_scroll_ratio:
        return False
    try:
        raw_page = await session.page._get_page()
        dur = r.uniform(settings.bio_browser_scroll_min_s, settings.bio_browser_scroll_max_s)
        steps = max(1, int(dur))
        for _ in range(steps):
            await raw_page.evaluate("window.scrollBy({top: 300, behavior: 'smooth'})")
            await asyncio.sleep(1.0)
        return True
    except Exception as e:
        logger.debug(f"[BioBrowser] micro-scroll saltato ({type(e).__name__}: {e})")
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_micro_scroll.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/browser_bio.py backend/tests/test_micro_scroll.py
git commit -m "feat(bio): maybe_micro_scroll (scroll umano ~35% dei profili)"
```

---

## Task 6: `scrape_bios_browser_session` — mini-sessione per-account

**Files:**
- Modify: `backend/app/services/browser_bio.py` (nuova funzione)
- Test: `backend/tests/test_scrape_bios_browser_session.py`

**Interfaces:**
- Consumes: `claim_next_pending` (Task 4), `fetch_and_store_bio_browser` (Task 3), `maybe_micro_scroll` (Task 5), `human_profile_pause`, `pick_session_cap` (da `app.services.scrape_bios`), `BrowserSession` (da `app.browser.context_manager`).
- Produces: `async def scrape_bios_browser_session(campaign_id: str, account_id: str) -> int | None` — apre una mini-sessione browser, scrapa fino a `pick_session_cap` profili claimati, chiude. Ritorna i **secondi di defer** per la pausa lunga (il task solleverà `Retry(defer=...)`), oppure `None` se pool esaurito / target raggiunto / halt.

**Nota:** questa funzione fa IO reale (browser). Il test la esercita con `BrowserSession` e i fetch **mockati** per verificare il flusso del loop (cap, claim, outcome, ritorno defer) senza aprire un browser.

- [ ] **Step 1: Write the failing test**

`backend/tests/test_scrape_bios_browser_session.py`:
```python
"""Mini-sessione browser: rispetta il cap, scrapa i claimati, ritorna il defer."""
from datetime import datetime
import pytest

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.services import browser_bio


class _FakeSession:
    def __init__(self, *a, **k): self.opened = False; self.closed = False
    async def open(self): self.opened = True
    async def close(self): self.closed = True
    class _P:
        async def ensure_logged_in(self, account_id): return None
    page = _P()


@pytest.mark.asyncio
async def test_session_scrapes_up_to_cap_and_returns_defer(monkeypatch):
    base = 960000000000 + int(datetime.utcnow().timestamp()) % 100000
    async with AsyncSessionLocal() as db:
        camp = Campaign(name="t", status=CampaignStatus.scraping, source_type="scrape")
        db.add(camp); await db.flush()
        for i in range(5):
            db.add(Follower(campaign_id=camp.id, ig_user_id=base + i,
                            username=f"u{base+i}", status=FollowerStatus.pending))
        await db.commit()
        cid = camp.id

    # cap piccolo per forzare il defer prima di esaurire i pending
    monkeypatch.setattr(browser_bio, "BrowserSession", _FakeSession)
    monkeypatch.setattr(browser_bio, "pick_session_cap", lambda *a, **k: 2)

    async def fake_fetch(follower, campaign, db, session):
        follower.status = FollowerStatus.bio_scraped
        follower.locked_by_account_id = None
        follower.locked_at = None
        await db.commit()
        return "done", None
    monkeypatch.setattr(browser_bio, "fetch_and_store_bio_browser", fake_fetch)
    monkeypatch.setattr(browser_bio, "human_profile_pause", lambda: _anoop())
    monkeypatch.setattr(browser_bio, "maybe_micro_scroll", lambda *a, **k: _anoop_false())

    defer = await browser_bio.scrape_bios_browser_session(cid, "acc-A")
    assert isinstance(defer, int) and defer >= 60      # pausa lunga → defer
    async with AsyncSessionLocal() as db:
        from sqlalchemy import select, func
        done = await db.scalar(
            select(func.count()).select_from(Follower).where(
                Follower.campaign_id == cid,
                Follower.status == FollowerStatus.bio_scraped))
        assert done == 2                                # esattamente il cap


async def _anoop(): return None
async def _anoop_false(): return False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_scrape_bios_browser_session.py -v`
Expected: FAIL — `AttributeError: ... scrape_bios_browser_session`.

- [ ] **Step 3: Implement `scrape_bios_browser_session`**

In `backend/app/services/browser_bio.py`. Assicura gli import a livello modulo: `from app.browser.context_manager import BrowserSession`, `from app.services.scrape_bios import pick_session_cap, bio_should_continue`. (Attenzione ai cicli d'import: `scrape_bios` importa da `browser_bio` solo dentro funzione — vedi Task 8 — quindi l'import a livello modulo qui è sicuro; se emerge un ciclo, sposta questi due import dentro la funzione.)
```python
async def scrape_bios_browser_session(campaign_id: str, account_id: str) -> int | None:
    """Una mini-sessione browser per UN account: apre, scrapa fino a un cap di
    profili claimati (pool disgiunto via claim_next_pending), chiude. Ritorna i
    secondi di defer per la pausa lunga anti-block, o None se non c'è più lavoro.
    Job corto: mai oltre job_timeout. Difensiva sui singoli profili."""
    from sqlalchemy import select, func
    from app.models.campaign import Campaign, CampaignStatus
    from app.models.follower import Follower, FollowerStatus
    from app.utils.events import emit as emit_event

    async with AsyncSessionLocal() as db:
        campaign = (await db.execute(
            select(Campaign).where(Campaign.id == campaign_id)
        )).scalar_one_or_none()
        if campaign is None or campaign.status not in (
            CampaignStatus.scraping, CampaignStatus.scraping_break
        ):
            return None
        if await is_halted(db):
            return None

        done = await db.scalar(
            select(func.count()).select_from(Follower).where(
                Follower.campaign_id == campaign_id,
                Follower.status == FollowerStatus.bio_scraped))
        if not bio_should_continue(campaign.bio_target, done or 0):
            return None

    cap = pick_session_cap(settings.bio_browser_session_cap_min, settings.bio_browser_session_cap_max)
    processed = 0
    session = None
    try:
        session = BrowserSession(account_id, headless=settings.bio_browser_headless)
        await session.open()
        await session.page.ensure_logged_in(account_id)

        while processed < cap:
            async with AsyncSessionLocal() as db:
                if await is_halted(db):
                    return None
                campaign = (await db.execute(
                    select(Campaign).where(Campaign.id == campaign_id)
                )).scalar_one_or_none()
                if campaign is None or campaign.status not in (
                    CampaignStatus.scraping, CampaignStatus.scraping_break
                ):
                    return None
                follower = await claim_next_pending(db, campaign_id, account_id)
                if follower is None:
                    return None  # pool globale esaurito
                try:
                    outcome, err = await fetch_and_store_bio_browser(follower, campaign, db, session)
                except Exception as e:
                    logger.warning(f"[BioBrowser] @{follower.username} errore inatteso ({e}) — skip")
                    outcome, err = "error", e

                if outcome == "done":
                    processed += 1
                    emit_event(campaign_id, "scrape_progress", f"@{follower.username} bio via browser")
                elif outcome in ("not_found", "private", "error"):
                    follower.status = FollowerStatus.skipped
                    follower.skip_reason = f"browser_{outcome}"
                    follower.locked_by_account_id = None
                    follower.locked_at = None
                    follower.updated_at = datetime.utcnow()
                    await db.commit()
                elif outcome in ("soft_block", "network"):
                    # non bruciare i pending: rilascia il claim, ferma la sessione
                    follower.locked_by_account_id = None
                    follower.locked_at = None
                    await db.commit()
                    logger.warning(f"[BioBrowser] stop sessione ({outcome}) su @{follower.username}: {err}")
                    emit_event(campaign_id, "scrape_stopped", f"Sessione browser fermata: {outcome}", level="warn")
                    return None

            await maybe_micro_scroll(session)
            await human_profile_pause()

        # cap raggiunto → pausa lunga anti-block via defer
        minutes = random.uniform(
            getattr(campaign, "scrape_break_minutes_min", 30) or 30,
            getattr(campaign, "scrape_break_minutes_max", 45) or 45,
        )
        emit_event(campaign_id, "scrape_break", f"Pausa bio browser {int(minutes)} min")
        return max(60, int(minutes * 60))
    except Exception as e:
        es = str(e).lower()
        logger.warning(f"[BioBrowser] mini-sessione @{account_id[:8]} fallita ({type(e).__name__}: {e})")
        # errore d'apertura/login: breve retry via defer, non perde i pending
        return 300
    finally:
        if session is not None:
            try:
                await session.close()
            except Exception:
                pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_scrape_bios_browser_session.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/browser_bio.py backend/tests/test_scrape_bios_browser_session.py
git commit -m "feat(bio): scrape_bios_browser_session (mini-sessione per-account, defer)"
```

---

## Task 7: Enqueue fan-out + task ARQ

**Files:**
- Modify: `backend/app/services/browser_bio.py` (`browser_bio_job_id`, `enqueue_browser_bio_workers`)
- Modify: `backend/app/workers/task_queue.py` (task `browser_bio_account_task` + registrazione)
- Test: `backend/tests/test_browser_bio_enqueue.py`

**Interfaces:**
- Consumes: `_scraping_accounts_of_campaign` (esistente), `arq_redis_settings` (da `app.services.work_enqueue`), `scrape_bios_browser_session` (Task 6).
- Produces:
  - `def browser_bio_job_id(campaign_id: str, account_id: str) -> str` → `f"biobrowser:{campaign_id}:{account_id}"`.
  - `async def enqueue_browser_bio_workers(campaign_id: str) -> int` → enqueue di un task per account scraping, con `_defer_by` stagger crescente e `_job_id` deterministico; ritorna il numero di task accodati.
  - `async def browser_bio_account_task(ctx, campaign_id: str, account_id: str) -> None` → task ARQ registrato in `WorkerSettings.functions` con `max_tries` alto (i defer sono normali).

- [ ] **Step 1: Write the failing test**

`backend/tests/test_browser_bio_enqueue.py`:
```python
"""Fan-out: un task ARQ per account, stagger via _defer_by, job_id deterministico."""
import pytest

from app.services import browser_bio


def test_job_id_deterministic():
    jid = browser_bio.browser_bio_job_id("camp1", "accA")
    assert jid == "biobrowser:camp1:accA"


@pytest.mark.asyncio
async def test_enqueue_one_task_per_account(monkeypatch):
    calls = []

    class _FakeRedis:
        async def enqueue_job(self, *args, **kwargs):
            calls.append((args, kwargs))

    async def fake_pool(*a, **k):
        return _FakeRedis()

    async def fake_accounts(campaign_id):
        return [("accA", "userA"), ("accB", "userB")]

    monkeypatch.setattr(browser_bio.arq, "create_pool", fake_pool)
    monkeypatch.setattr(browser_bio, "_scraping_accounts_of_campaign", fake_accounts)

    n = await browser_bio.enqueue_browser_bio_workers("camp1")
    assert n == 2
    assert len(calls) == 2
    # primo account: nessun defer (idx 0); secondo: defer > 0
    kwargs0, kwargs1 = calls[0][1], calls[1][1]
    assert kwargs0["_job_id"] == "biobrowser:camp1:accA"
    assert kwargs1["_job_id"] == "biobrowser:camp1:accB"
    assert kwargs0.get("_defer_by", 0) == 0
    assert kwargs1["_defer_by"] > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_browser_bio_enqueue.py -v`
Expected: FAIL — `AttributeError: ... browser_bio_job_id`.

- [ ] **Step 3: Implement enqueue in `browser_bio.py`**

Aggiungi in cima gli import: `import arq`, `from app.services.work_enqueue import arq_redis_settings, ARQ_MAIN_QUEUE`. (Entrambe le costanti vivono in `work_enqueue`, non in `task_queue` — importarle da lì evita un ciclo d'import, dato che `task_queue` importa `browser_bio`.) Poi:
```python
def browser_bio_job_id(campaign_id: str, account_id: str) -> str:
    return f"biobrowser:{campaign_id}:{account_id}"


async def enqueue_browser_bio_workers(campaign_id: str) -> int:
    """Fan-out: un task ARQ per account scraping, con stagger crescente via
    _defer_by (partenze differite) e _job_id deterministico (dedup, come i DM).
    Ritorna il numero di task accodati."""
    accounts = await _scraping_accounts_of_campaign(campaign_id)
    if not accounts:
        return 0
    redis = await arq.create_pool(arq_redis_settings())
    lo = min(settings.bio_browser_stagger_min_s, settings.bio_browser_stagger_max_s)
    hi = max(settings.bio_browser_stagger_min_s, settings.bio_browser_stagger_max_s)
    n = 0
    for idx, (account_id, _username) in enumerate(accounts):
        defer = 0 if idx == 0 else int(random.uniform(lo, hi) * idx)
        await redis.enqueue_job(
            "browser_bio_account_task",
            campaign_id,
            account_id,
            _job_id=browser_bio_job_id(campaign_id, account_id),
            _defer_by=defer,
            _queue_name=ARQ_MAIN_QUEUE,
        )
        n += 1
    return n
```

- [ ] **Step 4: Run enqueue test**

Run: `pytest tests/test_browser_bio_enqueue.py -v`
Expected: PASS.

- [ ] **Step 5: Implement the ARQ task in `task_queue.py`**

In `backend/app/workers/task_queue.py`, accanto agli altri task (es. dopo `scrape_bios_task`):
```python
async def browser_bio_account_task(ctx: dict, campaign_id: str, account_id: str) -> None:
    """ARQ task: mini-sessione bio via browser per UN account. Job corto; la
    pausa lunga è un Retry(defer). Un errore di questo account non tocca gli altri."""
    from arq.worker import Retry
    from app.services.browser_bio import scrape_bios_browser_session
    from app.utils.db_resilience import is_transient_db_error
    logger.info(f"[ARQ] browser_bio_account_task start campaign={campaign_id} account={account_id}")
    try:
        defer = await scrape_bios_browser_session(campaign_id, account_id)
        if defer:
            logger.info(f"[ARQ] browser_bio_account_task pausa — defer {defer}s")
            raise Retry(defer=defer)
    except Retry:
        raise
    except Exception as e:
        if is_transient_db_error(e):
            raise Retry(defer=60)
        logger.exception(f"[ARQ] browser_bio_account_task failed: {e}")
        raise
```
Registralo in `WorkerSettings.functions` con `max_tries` alto (i defer ripetuti sono il funzionamento normale, come `scrape_bios_task`):
```python
        func(browser_bio_account_task, max_tries=10000),
```

- [ ] **Step 6: Verify the task imports and registers**

Run: `python -c "from app.workers.task_queue import WorkerSettings, browser_bio_account_task; print([getattr(f,'coroutine',f).__name__ for f in WorkerSettings.functions])"`
Expected: la lista include `browser_bio_account_task`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/browser_bio.py backend/app/workers/task_queue.py backend/tests/test_browser_bio_enqueue.py
git commit -m "feat(bio): fan-out enqueue per-account + task ARQ browser_bio_account_task"
```

---

## Task 8: Biforcazione in `scrape_bios` + gate attività browser API

**Files:**
- Modify: `backend/app/services/scrape_bios.py` (biforcazione in `scrape_bios`, ~riga 70; gate su `run_pause_browser_all_accounts`, ~riga 278)
- Test: `backend/tests/test_scrape_bios_browser_fork.py`

**Interfaces:**
- Consumes: `enqueue_browser_bio_workers` (Task 7).
- Produces: quando `campaign.bio_engine == 'browser'`, `scrape_bios(campaign_id)` fa il fan-out e ritorna `None` **senza** entrare nel loop API.

- [ ] **Step 1: Write the failing test**

`backend/tests/test_scrape_bios_browser_fork.py`:
```python
"""Biforcazione: bio_engine=browser → fan-out, niente loop API."""
from datetime import datetime
import pytest

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus
from app.services import scrape_bios as sb


@pytest.mark.asyncio
async def test_browser_engine_calls_enqueue_and_skips_api(monkeypatch):
    async with AsyncSessionLocal() as db:
        camp = Campaign(name="t", status=CampaignStatus.scraping,
                        source_type="scrape", bio_engine="browser")
        db.add(camp); await db.commit()
        cid = camp.id

    called = {"enqueue": 0, "pool": 0}

    async def fake_enqueue(campaign_id):
        called["enqueue"] += 1
        assert campaign_id == cid
        return 1
    # se entrasse nel path API costruirebbe la ScrapingPool → lo intercettiamo
    async def fake_build(*a, **k):
        called["pool"] += 1
        raise AssertionError("non deve entrare nel path API")

    monkeypatch.setattr("app.services.browser_bio.enqueue_browser_bio_workers", fake_enqueue)
    monkeypatch.setattr(sb.ScrapingPool, "build", staticmethod(fake_build))

    result = await sb.scrape_bios(cid)
    assert result is None
    assert called["enqueue"] == 1
    assert called["pool"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_scrape_bios_browser_fork.py -v`
Expected: FAIL — entra nel path API (AssertionError) o `enqueue==0`.

- [ ] **Step 3: Add the fork in `scrape_bios`**

In `backend/app/services/scrape_bios.py`, dopo i guard di stato/halt e prima della gestione `scraping_break`/costruzione pool (dopo il blocco ~riga 78 che valida lo status, prima di `pool = await ScrapingPool.build(...)`), aggiungi:
```python
        if campaign.bio_engine == 'browser':
            from app.services.browser_bio import enqueue_browser_bio_workers
            n = await enqueue_browser_bio_workers(campaign_id)
            from app.utils.events import emit as emit_event
            if n == 0:
                campaign.status = CampaignStatus.error
                campaign.scrape_outcome = "scrape_no_account"
                campaign.updated_at = datetime.utcnow()
                await db.commit()
                emit_event(campaign_id, "scrape_stopped", "Nessun account scraping per il motore browser", level="error")
            else:
                emit_event(campaign_id, "scrape_start", f"Fase Bio via browser — {n} account in parallelo")
            return None
```
Posizionalo in modo che le sessioni parallele governino da sé lo stato; `scrape_bios` non deve proseguire nel loop API. Verifica il punto esatto leggendo le righe attorno con `sed -n '60,100p' backend/app/services/scrape_bios.py`.

- [ ] **Step 4: Gate the pause-time browser activity (motore API)**

Sempre in `scrape_bios.py`, dove chiama `run_pause_browser_all_accounts` (~riga 278), avvolgi nella condizione che escluda il motore browser (evita doppioni):
```python
                        if campaign.bio_engine != 'browser':
                            try:
                                from app.services.browser_bio import run_pause_browser_all_accounts
                                spent_seconds = await run_pause_browser_all_accounts(campaign_id)
                            except Exception as e:
                                logger.warning(f"[Bio] attivita' browser in pausa fallita (ignoro): {e}")
```
(Mantieni `spent_seconds = 0` inizializzato sopra come già è.)

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_scrape_bios_browser_fork.py -v`
Expected: PASS.

- [ ] **Step 6: Regression — path API intatto**

Run: `pytest tests/test_bio_micro_yield.py tests/test_bio_error_no_infinite_loop.py tests/test_bio_delay_variance.py -v`
Expected: PASS (path API non toccato nel comportamento).

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/scrape_bios.py backend/tests/test_scrape_bios_browser_fork.py
git commit -m "feat(bio): biforcazione scrape_bios su bio_engine + gate attività browser API"
```

---

## Task 9: Frontend — dropdown "Motore Fase Bio"

**Files:**
- Modify: form di creazione/modifica campagna nel frontend (individua con `grep -rn "inbox_engine\|scrape_mode" frontend/ --include=*.tsx`)

**Interfaces:**
- Consumes: campo `bio_engine` in `CampaignCreate`/`CampaignResponse` (Task 1). Valori `api` | `browser`.

- [ ] **Step 1: Locate the campaign form field**

Run: `grep -rn "inbox_engine\|scrape_mode\|bio_fetch_delay" frontend/ --include=*.tsx | head`
Identifica il componente form dove vivono i campi di scraping della campagna. Segui esattamente il pattern di un dropdown esistente (`scrape_mode` è l'analogo migliore: enum a 2 valori).

- [ ] **Step 2: Add the dropdown**

Replica il markup del dropdown `scrape_mode` per `bio_engine`, con opzioni:
- `API (veloce)` → valore `api`
- `Browser (prudente)` → valore `browser`

Default selezionato: `api`. Aggiungi `bio_engine` al payload inviato al backend (stesso oggetto che già include `scrape_mode`/`inbox_engine`) e al tipo TypeScript della campagna se tipizzato (cerca `scrape_mode` nei tipi con `grep -rn "scrape_mode" frontend/ --include=*.ts`).

- [ ] **Step 3: Verify build**

Run (da `frontend/`): `npm run build`
Expected: build senza errori di tipo su `bio_engine`.

- [ ] **Step 4: Manual smoke (dev)**

Avvia `npm run dev`, apri la form campagna, verifica che il dropdown "Motore Fase Bio" appaia con default `API` e che creando una campagna con `Browser` il valore arrivi al backend (Network tab → payload contiene `bio_engine: "browser"`).

- [ ] **Step 5: Commit**

```bash
git add frontend/
git commit -m "feat(bio): dropdown Motore Fase Bio (api/browser) nella form campagna"
```

---

## Task 10: End-to-end reale + aggiornamento contesto

**Files:**
- Modify: `docs/project/PROGRESS.md`, `INDEX.md`, `CLAUDE.md`
- Memory: `C:\Users\39342\.claude\projects\d--BOT-OUTBOUND\memory\project_state.md`

- [ ] **Step 1: Full test suite**

Run (da `backend/`): `pytest tests/ -q`
Expected: tutti i nuovi test verdi, nessuna regressione. Annota eventuali fallimenti pre-esistenti non correlati.

- [ ] **Step 2: End-to-end reale (test manuale, seguendo lo spec §12)**

Prerequisiti dello spec §13.1: account **solo-scraping** (o `messaging_enabled=False`), `bio_browser_headless=false` nel `.env`, `max_concurrent_browsers`/`max_jobs` a 1-2. Crea una campagna `bio_engine=browser` con ~10 profili in lista (`pending`), avvia la Fase Bio. Osserva:
- il browser si apre e naviga i profili a ritmo umano;
- i dati scritti coincidono con quelli del path API (bio, contatti) — confronta su un paio di profili noti;
- nessun consumo del cap API (log `no cap API`);
- con 2 account: pool disgiunti (nessun follower `bio_scraped` due volte — query `SELECT username, count(*) FROM followers WHERE campaign_id=... GROUP BY username HAVING count(*)>1` deve essere vuota).

Documenta l'esito (tempi/profilo osservati, anomalie) nella sezione datata di `project_state.md`.

- [ ] **Step 3: Update project docs**

- `docs/project/PROGRESS.md`: sezione datata con cosa è stato aggiunto (motore bio browser, file toccati, comportamento).
- `INDEX.md`: nuova fase nella tabella "Fasi completate" + `browser_bio.py` aggiornato nella tabella file critici se cambia il ruolo.
- `CLAUDE.md`: nota nella sezione scraping che la Fase Bio ha due motori selezionabili (`bio_engine`).
- `memory/project_state.md`: sezione datata (come da regola obbligatoria del CLAUDE del progetto).

- [ ] **Step 4: Commit**

```bash
git add docs/ INDEX.md CLAUDE.md
git commit -m "docs(bio): motore Fase Bio via browser — PROGRESS/INDEX/CLAUDE + esito test"
```

- [ ] **Step 5: Push + PR**

```bash
git push -u origin feat/bio-scraping-browser-mode
gh pr create --title "Fase Bio via browser (bio_engine selezionabile)" --body "Motore bio alternativo via Patchright, multi-account parallelo, pool disgiunti. Spec: docs/superpowers/specs/2026-07-06-bio-scraping-browser-mode-design.md"
```

---

## Self-Review (esito)

**Spec coverage:** §5 modello/migration→T1; §9 config→T2; C2 lock→T3; §6.4 claim→T4; §6.5 scroll→T5; §6.3 mini-sessione→T6; §6.2 fan-out + task→T7; §6.1 biforcazione + §13 gate→T8; §10 frontend→T9; §11/§12/§13.1 test+docs→T10. Nessuna sezione dello spec resta senza task.

**Type consistency:** `claim_next_pending(db, campaign_id, account_id) -> Follower|None` usato in T6; `scrape_bios_browser_session(campaign_id, account_id) -> int|None` usato in T7 task; `enqueue_browser_bio_workers(campaign_id) -> int` usato in T8; `browser_bio_job_id` coerente T7. `maybe_micro_scroll(session, *, rng=None)` coerente T5↔T6. Nomi task ARQ per stringa (`"browser_bio_account_task"`) coerenti tra enqueue (T7) e registrazione (T7).

**Placeholder scan:** nessun TBD/TODO; ogni step ha codice o comando concreto. Punti che richiedono verifica in-loco (nome costante `ARQ_MAIN_QUEUE`, riga esatta della biforcazione, posizione dropdown frontend) sono marcati con il `grep`/`sed` da eseguire, non lasciati vaghi.

**Rischi noti da tenere d'occhio in esecuzione:** (a) possibili cicli d'import `scrape_bios`↔`browser_bio` (mitigati con import dentro funzione dove serve); (b) `max_concurrent_browsers` non più semaforo in-process con task ARQ separati → per il test si limita `max_jobs` (spec §13); (c) `bio_session_cap_min/max` devono esistere in config (già usati dal path API) — verificare in T6.
