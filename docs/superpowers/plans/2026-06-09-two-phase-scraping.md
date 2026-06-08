# Two-Phase Scraping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separare lo scraping `source_type=scrape` in due fasi indipendenti — Fase Lista (raccolta info base, paced, senza cap) e Fase Bio (estrazione bio/contatti sotto cap) — con avvio/stop/target/ripresa propri e interleaving.

**Architecture:** Due job ARQ distinti (`list_followers_task`, `scrape_bios_task`) leggono/scrivono stato dal DB (`Follower.status`: `pending` = solo lista, `bio_scraped` = bio fatta). Nuovi stati campagna `listing`/`listing_break` espliciti. `ScrapingPool`, challenge handler, session break e cap sono riusati da entrambe le fasi. Import e flusso DM a valle invariati.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2 async, Alembic, ARQ, instagrapi, Next.js 14 + TypeScript. Test: pytest + pytest-asyncio con fake objects (no live DB/IG).

**Spec di riferimento:** `docs/superpowers/specs/2026-06-08-two-phase-scraping-design.md`

**Nota su modifiche non committate di questa sessione:** in `scraper.py` esistono già (a) l'handler challenge nel blocco `except Exception` di `scrape_followers`, (b) `_fetch_followers_chunk` che passa `max_amount=amount`. In `config.py` esistono `scrape_page_*` e `scrape_page_size`. Questo piano le riusa/riallinea: il challenge handler va spostato in un helper condiviso, le `scrape_page_*` diventano `list_page_*`.

---

## File Structure

**Backend — modificati:**
- `backend/alembic/versions/016_two_phase_scraping.py` (Create) — colonne `list_target`, `bio_target`
- `backend/app/models/campaign.py` (Modify) — enum `listing`/`listing_break`, colonne target
- `backend/app/schemas/campaign.py` (Modify) — `list_target`, `bio_target`, `list_progress`, `bio_progress`
- `backend/app/config.py` (Modify) — cap 300, `list_page_*`, rimozione `scrape_page_*`
- `backend/app/services/scraper.py` (Modify) — split in fase lista / fase bio + helper challenge condiviso
- `backend/app/services/scrape_list.py` (Create) — funzione Fase Lista
- `backend/app/services/scrape_bios.py` (Create) — funzione Fase Bio
- `backend/app/workers/list_worker.py` (Create) — `list_followers_task`
- `backend/app/workers/bio_worker.py` (Create) — `scrape_bios_task`
- `backend/app/workers/task_queue.py` (Modify) — registra i due task
- `backend/app/services/work_enqueue.py` (Modify) — `enqueue_list`, `enqueue_bios`, job-id helpers, startup-guard stati
- `backend/app/api/campaigns.py` (Modify) — 4 endpoint + progress in `_enrich_campaign` + redirect `start-scrape`

**Frontend — modificati:**
- `frontend/lib/types.ts` (Modify) — stati + campi progress/target
- `frontend/lib/api.ts` (Modify) — 4 metodi
- `frontend/app/campaigns/[id]/page.tsx` (Modify) — due pannelli

**Test — creati:**
- `backend/tests/test_two_phase_enqueue.py`
- `backend/tests/test_list_page_sizing.py`
- `backend/tests/test_two_phase_status.py`
- `backend/tests/test_campaign_progress.py`

---

## Task 1: Config — cap 300 + parametri Fase Lista

**Files:**
- Modify: `backend/app/config.py`
- Test: `backend/tests/test_list_page_sizing.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_list_page_sizing.py
"""Fase Lista: dimensione pagina e delay devono leggere dai settings con bound corretti."""
from app.config import settings


def test_cap_default_raised_to_300():
    assert settings.scrape_daily_limit == 300


def test_list_page_settings_present_and_sane():
    assert settings.list_page_size_min == 20
    assert settings.list_page_size_max == 40
    assert settings.list_page_size_min <= settings.list_page_size_max
    assert settings.list_page_delay_min_seconds == 5
    assert settings.list_page_delay_max_seconds == 10
    assert 0.0 <= settings.list_long_pause_probability <= 1.0
    assert settings.list_long_pause_min_seconds <= settings.list_long_pause_max_seconds
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ./venv/Scripts/python.exe -m pytest tests/test_list_page_sizing.py -v`
Expected: FAIL (`scrape_daily_limit == 180`, `list_page_size_min` AttributeError)

- [ ] **Step 3: Edit config.py**

In `backend/app/config.py`, cambiare il default del cap:

```python
    # Max user_info lookups/day/account for scraping (anti-ban). Per-campaign override on campaigns.scrape_daily_limit.
    scrape_daily_limit: int = 300
```

Rimuovere il blocco `scrape_page_size` / `scrape_page_delay_*` / `scrape_page_long_pause_*` introdotto in precedenza e sostituirlo con i parametri Fase Lista:

```python
    # Fase Lista: dimensione pagina randomizzata passata come max_amount a
    # user_followers_v1_chunk. Senza un valore piccolo, instagrapi drena l'intera
    # lista in un burst count=200 senza delay -> challenge IG. Con 20-40 ogni
    # chiamata ritorna pochi utenti e il delay sotto agisce (scroll umano).
    list_page_size_min: int = 20
    list_page_size_max: int = 40
    # Delay tra pagine lista (lognormale, non uniforme).
    list_page_delay_min_seconds: int = 5
    list_page_delay_max_seconds: int = 10
    # Pausa lunga occasionale tra pagine lista (scroll che si ferma).
    list_long_pause_probability: float = 0.06   # ~ogni 15-20 pagine
    list_long_pause_min_seconds: int = 30
    list_long_pause_max_seconds: int = 60
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ./venv/Scripts/python.exe -m pytest tests/test_list_page_sizing.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/config.py backend/tests/test_list_page_sizing.py
git commit -m "feat(config): cap 300 + parametri Fase Lista (two-phase scraping)"
```

---

## Task 2: Model — stati campagna + colonne target

**Files:**
- Modify: `backend/app/models/campaign.py:9-18` (enum), colonne ~`:50-64`
- Test: `backend/tests/test_two_phase_status.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_two_phase_status.py
"""Two-phase: nuovi stati campagna e colonne target."""
from app.models.campaign import Campaign, CampaignStatus


def test_new_statuses_exist():
    assert CampaignStatus.listing.value == "listing"
    assert CampaignStatus.listing_break.value == "listing_break"


def test_target_columns_present():
    cols = Campaign.__table__.columns.keys()
    assert "list_target" in cols
    assert "bio_target" in cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ./venv/Scripts/python.exe -m pytest tests/test_two_phase_status.py -v`
Expected: FAIL (`listing` AttributeError)

- [ ] **Step 3: Edit campaign.py**

Nell'enum `CampaignStatus` aggiungere i due stati dopo `draft`:

```python
class CampaignStatus(str, enum.Enum):
    draft = "draft"
    listing = "listing"
    listing_break = "listing_break"
    scraping = "scraping"
    scraping_break = "scraping_break"
    scraping_and_running = "scraping_and_running"
    ready = "ready"
    running = "running"
    paused = "paused"
    completed = "completed"
    error = "error"
```

Dopo la colonna `scrape_cursor` (riga ~62) aggiungere:

```python
    list_target: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bio_target: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ./venv/Scripts/python.exe -m pytest tests/test_two_phase_status.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/campaign.py backend/tests/test_two_phase_status.py
git commit -m "feat(model): stati listing + colonne target (two-phase scraping)"
```

---

## Task 3: Migration 016 — colonne target

**Files:**
- Create: `backend/alembic/versions/016_two_phase_scraping.py`

- [ ] **Step 1: Create migration file**

```python
# backend/alembic/versions/016_two_phase_scraping.py
"""Two-phase scraping: list_target / bio_target on campaigns.

Revision ID: 016
Revises: 015
Create Date: 2026-06-09
"""
from alembic import op
import sqlalchemy as sa

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("campaigns", sa.Column("list_target", sa.Integer(), nullable=True))
    op.add_column("campaigns", sa.Column("bio_target", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("campaigns", "bio_target")
    op.drop_column("campaigns", "list_target")
```

- [ ] **Step 2: Verify migration imports cleanly**

Run: `cd backend && ./venv/Scripts/python.exe -c "import importlib.util, sys; spec=importlib.util.spec_from_file_location('m','alembic/versions/016_two_phase_scraping.py'); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); print('rev', m.revision, 'down', m.down_revision)"`
Expected: `rev 016 down 015`

- [ ] **Step 3: Apply migration to Supabase**

Run: `cd backend && ./venv/Scripts/python.exe -m scripts.migrate`
Expected: log "Running upgrade 015 -> 016" senza errori. (Se va in timeout: fermare bot/backend zombie che tengono lock su `campaigns`, vedi CLAUDE.md.)

- [ ] **Step 4: Commit**

```bash
git add backend/alembic/versions/016_two_phase_scraping.py
git commit -m "feat(migration): 016 list_target/bio_target su campaigns"
```

---

## Task 4: Helper challenge condiviso

**Files:**
- Modify: `backend/app/services/scraper.py` (estrarre handler challenge in funzione riusabile)
- Test: `backend/tests/test_two_phase_status.py` (append)

Razionale: l'handler challenge è già nel blocco `except Exception` di `scrape_followers`. Va estratto in una funzione pura riusabile da entrambe le fasi nuove.

- [ ] **Step 1: Write the failing test (append al file esistente)**

```python
# append a backend/tests/test_two_phase_status.py
def test_is_challenge_exception_detects_by_name():
    from app.services.scraper import is_challenge_exception

    class ChallengeResolve(Exception):
        pass

    class SomethingElse(Exception):
        pass

    assert is_challenge_exception(ChallengeResolve("x")) is True
    assert is_challenge_exception(SomethingElse("x")) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ./venv/Scripts/python.exe -m pytest tests/test_two_phase_status.py::test_is_challenge_exception_detects_by_name -v`
Expected: FAIL (ImportError `is_challenge_exception`)

- [ ] **Step 3: Add helper in scraper.py**

In `backend/app/services/scraper.py`, vicino agli altri helper module-level, aggiungere:

```python
def is_challenge_exception(exc: Exception) -> bool:
    """instagrapi usa nomi classe che contengono 'Challenge' per checkpoint/2FA."""
    return "Challenge" in type(exc).__name__


async def isolate_challenged_account(db, campaign, account, exc: Exception) -> None:
    """Isola l'account challenged e mette la campagna in pausa (riprendibile).

    Usata da Fase Lista e Fase Bio: una challenge IG NON deve lasciare l'account
    'active' (ogni retry rifallirebbe) ne' mandare la campagna in 'error' secco.
    """
    from sqlalchemy import select
    from app.models.account import InstagramAccount, AccountStatus
    from app.models.activity_log import ActivityLog
    from app.utils.events import emit as emit_event

    exc_name = type(exc).__name__
    acc = None
    if account is not None:
        acc = (await db.execute(
            select(InstagramAccount).where(InstagramAccount.id == account.id)
        )).scalar_one_or_none()
    acc_label = acc.username if acc else "?"
    if acc:
        acc.status = AccountStatus.challenge_required
    campaign.status = CampaignStatus.paused
    campaign.scrape_outcome = "challenge"
    campaign.updated_at = datetime.utcnow()
    db.add(ActivityLog(
        campaign_id=campaign.id,
        action="challenge",
        details=json.dumps({"account": acc_label, "exc": exc_name}),
    ))
    await db.commit()
    logger.error(f"[Scraper] Challenge IG su @{acc_label} ({exc_name}) — account isolato, campagna in pausa")
    emit_event(
        campaign.id, "scrape_stopped",
        f"Instagram richiede verifica su @{acc_label}. Risolvi la challenge (app/web IG), poi ri-login browser e riavvia.",
        level="error",
    )
```

Poi nel blocco `except Exception as e:` di `scrape_followers` sostituire la logica inline introdotta in precedenza con:

```python
        except Exception as e:
            if is_challenge_exception(e) and locals().get("account") is not None:
                await isolate_challenged_account(db, campaign, locals().get("account"), e)
            else:
                logger.error(f"Scrape failed for campaign {campaign_id}: {e}")
                campaign.status = CampaignStatus.error
                await db.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ./venv/Scripts/python.exe -m pytest tests/test_two_phase_status.py -v`
Expected: PASS (entrambi i test del file)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/scraper.py backend/tests/test_two_phase_status.py
git commit -m "refactor(scraper): helper challenge condiviso (is_challenge_exception/isolate_challenged_account)"
```

---

## Task 5: Servizio Fase Lista

**Files:**
- Create: `backend/app/services/scrape_list.py`
- Test: `backend/tests/test_list_page_sizing.py` (append)

Responsabilità: paginare la lista follower a blocchetti `random(20,40)`, creare `Follower(status=pending)` con info base (NO `user_info_v1`), salvare cursore, rispettare `list_target`, gestire stop/pausa/challenge. Riusa `ScrapingPool` e `_fetch_followers_chunk` da `scraper.py`.

- [ ] **Step 1: Write the failing test (pure helpers)**

```python
# append a backend/tests/test_list_page_sizing.py
def test_next_page_size_within_bounds():
    from app.services.scrape_list import next_page_size
    for _ in range(200):
        n = next_page_size()
        assert 20 <= n <= 40


def test_list_remaining_respects_target():
    from app.services.scrape_list import remaining_for_target
    # target None = illimitato -> ritorna il page size proposto
    assert remaining_for_target(target=None, already=100, page=30) == 30
    # target 500, gia' 480 -> al massimo 20
    assert remaining_for_target(target=500, already=480, page=30) == 20
    # target raggiunto -> 0
    assert remaining_for_target(target=500, already=500, page=30) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ./venv/Scripts/python.exe -m pytest tests/test_list_page_sizing.py -v`
Expected: FAIL (ImportError `scrape_list`)

- [ ] **Step 3: Create scrape_list.py**

```python
# backend/app/services/scrape_list.py
"""Fase Lista: raccoglie solo info base dei follower a blocchetti paced.

NON chiama user_info_v1 (nessun consumo di cap). Crea Follower(status=pending)
che la Fase Bio (scrape_bios.py) processera' poi. Riusa ScrapingPool, il challenge
handler e _fetch_followers_chunk dello scraper esistente.
"""
import asyncio
import random
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import func, select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.services.bot_state_service import is_halted
from app.services.scraping_pool import ScrapingPool, ScrapingPoolEmpty
from app.services.scraper import _fetch_followers_chunk, is_challenge_exception, isolate_challenged_account
from app.utils.events import emit as emit_event
from app.utils.exceptions import BotHaltedError, ScrapeBudgetError, ScraperError, TargetPrivateError


def next_page_size() -> int:
    """Dimensione pagina lista randomizzata nei bound di config."""
    return random.randint(settings.list_page_size_min, settings.list_page_size_max)


def remaining_for_target(target: int | None, already: int, page: int) -> int:
    """Quanti follower richiedere in questa pagina dato il target.

    target None = illimitato -> page intero. Altrimenti clamp a (target-already), min 0.
    """
    if target is None:
        return page
    return max(0, min(page, target - already))


async def _list_page_delay() -> None:
    """Delay lognormale tra pagine + pausa lunga occasionale."""
    if random.random() < settings.list_long_pause_probability:
        delay = random.uniform(settings.list_long_pause_min_seconds, settings.list_long_pause_max_seconds)
        logger.info(f"[Lista] Pausa lunga {delay:.0f}s (scroll fermo)")
    else:
        lo, hi = settings.list_page_delay_min_seconds, settings.list_page_delay_max_seconds
        mid = (lo + hi) / 2
        delay = min(hi, max(lo, random.lognormvariate(0, 0.4) * mid))
    await asyncio.sleep(delay)


async def list_followers(campaign_id: str) -> None:
    """Entry point Fase Lista. Chiamata dal worker."""
    async with AsyncSessionLocal() as db:
        campaign = (await db.execute(select(Campaign).where(Campaign.id == campaign_id))).scalar_one_or_none()
        if not campaign:
            logger.error(f"[Lista] Campaign {campaign_id} not found")
            return
        if campaign.status not in (CampaignStatus.listing, CampaignStatus.listing_break):
            logger.info(f"[Lista] Campaign status='{campaign.status.value}' — skip stale retry")
            return
        if await is_halted(db):
            emit_event(campaign_id, "scrape_stopped", "Bot in pausa globale — lista non avviata", level="warn")
            return

        scrape_mode = getattr(campaign, "scrape_mode", "followers")
        mode_label = "following" if scrape_mode == "following" else "follower"
        pool = None
        account = None
        try:
            pool = await ScrapingPool.build(db, campaign)
            sel = pool.next(campaign)
            if sel is None:
                raise ScrapeBudgetError("Nessun account scraping disponibile")
            account, client = sel

            # Risolvi target se non gia' fatto
            if not campaign.target_user_id:
                target_user = await asyncio.to_thread(client.user_info_by_username_v1, campaign.target_username)
                if target_user.is_private:
                    raise TargetPrivateError(f"@{campaign.target_username} privato")
                campaign.target_user_id = target_user.pk
                await db.commit()

            emit_event(campaign_id, "scrape_start", f"Fase Lista avviata ({mode_label}) — target {campaign.list_target or 'tutta la lista'}")
            already = await db.scalar(select(func.count(Follower.id)).where(Follower.campaign_id == campaign_id)) or 0
            max_id = campaign.scrape_cursor or None
            since_break = 0

            while True:
                if await is_halted(db):
                    raise BotHaltedError("kill-switch")
                await db.refresh(campaign)
                if campaign.status not in (CampaignStatus.listing, CampaignStatus.listing_break):
                    logger.info(f"[Lista] Stato '{campaign.status.value}' — interrotto a {already}")
                    return
                # Target raggiunto?
                page = remaining_for_target(campaign.list_target, already, next_page_size())
                if page == 0:
                    logger.info(f"[Lista] Target {campaign.list_target} raggiunto ({already})")
                    break

                await _list_page_delay()
                batch, max_id = await asyncio.to_thread(
                    _fetch_followers_chunk, client, campaign.target_user_id, page, max_id, scrape_mode
                )
                if not batch:
                    logger.info(f"[Lista] Lista IG esaurita ({already})")
                    break

                stored = 0
                for us in batch:
                    exists = await db.scalar(
                        select(Follower.id).where(
                            Follower.campaign_id == campaign_id,
                            Follower.ig_user_id == int(us.pk),
                        )
                    )
                    if exists:
                        continue
                    db.add(Follower(
                        campaign_id=campaign_id,
                        ig_user_id=int(us.pk),
                        username=us.username,
                        full_name=us.full_name,
                        is_private=us.is_private,
                        is_verified=getattr(us, "is_verified", False) or False,
                        profile_pic_url=str(us.profile_pic_url) if us.profile_pic_url else None,
                        status=FollowerStatus.pending,
                    ))
                    stored += 1
                already += stored
                since_break += stored
                campaign.scrape_cursor = max_id
                campaign.total_followers = already
                campaign.updated_at = datetime.utcnow()
                await db.commit()
                emit_event(campaign_id, "scrape_batch", f"Lista: {already}" + (f"/{campaign.list_target}" if campaign.list_target else ""))

                if not max_id:
                    logger.info(f"[Lista] Fine lista ({already})")
                    break

                # Pausa sessione lista
                if since_break >= getattr(campaign, "scrape_session_size", 250):
                    minutes = random.uniform(
                        getattr(campaign, "scrape_break_minutes_min", 30),
                        getattr(campaign, "scrape_break_minutes_max", 45),
                    )
                    campaign.scrape_break_prev_status = CampaignStatus.listing.value
                    campaign.status = CampaignStatus.listing_break
                    campaign.scrape_break_until = datetime.utcnow() + timedelta(minutes=minutes)
                    campaign.updated_at = datetime.utcnow()
                    await db.commit()
                    emit_event(campaign_id, "scrape_break", f"Pausa lista {int(minutes)} min dopo {already}")
                    return  # il resume riaccoda il job

            # Completata: torna a ready (o resta listing-done -> ready)
            campaign.status = CampaignStatus.ready
            campaign.scrape_cursor = None
            campaign.updated_at = datetime.utcnow()
            await db.commit()
            emit_event(campaign_id, "scrape_complete", f"Fase Lista completata: {already} follower in lista")

        except BotHaltedError:
            campaign.status = CampaignStatus.paused
            campaign.updated_at = datetime.utcnow()
            await db.commit()
            emit_event(campaign_id, "scrape_stopped", "Bot in pausa globale — lista interrotta", level="warn")
        except (ScrapeBudgetError, ScrapingPoolEmpty, TargetPrivateError, ScraperError) as e:
            campaign.status = CampaignStatus.error
            await db.commit()
            emit_event(campaign_id, "scrape_stopped", f"Fase Lista non avviata: {e}", level="error")
        except Exception as e:
            if is_challenge_exception(e) and account is not None:
                await isolate_challenged_account(db, campaign, account, e)
            else:
                logger.error(f"[Lista] Errore campaign {campaign_id}: {e}")
                campaign.status = CampaignStatus.error
                await db.commit()
        finally:
            if pool is not None:
                try:
                    await pool.save_sessions(db)
                except Exception as exc:
                    logger.warning(f"[Lista] save_sessions fallito: {exc}")
                await pool.release()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ./venv/Scripts/python.exe -m pytest tests/test_list_page_sizing.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/scrape_list.py backend/tests/test_list_page_sizing.py
git commit -m "feat(scraper): Fase Lista (scrape_list) — paginazione paced senza bio"
```

---

## Task 6: Servizio Fase Bio

**Files:**
- Create: `backend/app/services/scrape_bios.py`
- Modify: `backend/app/services/scraper.py` (estrarre il loop bio per-follower in helper riusabile `fetch_and_store_bio`)
- Test: `backend/tests/test_two_phase_status.py` (append)

Responsabilità: ciclare i `Follower(status=pending)`, per ognuno `user_info_v1` → bio+contatti → `bio_scraped`, sotto cap, con `bio_target`, session break, rotazione pool. Riusa la logica esistente di `_store_followers_batch` estraendone il corpo per-follower in un helper.

- [ ] **Step 1: Write the failing test (pure helper)**

```python
# append a backend/tests/test_two_phase_status.py
def test_bio_remaining_respects_target():
    from app.services.scrape_bios import bio_should_continue
    # target None -> continua finche' ci sono pending
    assert bio_should_continue(target=None, done=50) is True
    # target 200, done 199 -> continua
    assert bio_should_continue(target=200, done=199) is True
    # target raggiunto -> stop
    assert bio_should_continue(target=200, done=200) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ./venv/Scripts/python.exe -m pytest tests/test_two_phase_status.py::test_bio_remaining_respects_target -v`
Expected: FAIL (ImportError `scrape_bios`)

- [ ] **Step 3a: Estrarre helper bio in scraper.py**

In `backend/app/services/scraper.py`, estrarre il corpo del `for user_short in followers_batch:` di `_store_followers_batch` (la parte b→g: `user_info_v1`, `extract_contacts`, increment cap, create/update Follower, upsert_lead, delay) in una funzione riusabile che opera su UN follower già esistente nel DB:

```python
async def fetch_and_store_bio(follower, campaign, db, pool) -> str:
    """Estrae bio+contatti per UN follower gia' in DB (status pending) e lo porta
    a bio_scraped. Ritorna esito: 'done' | 'soft_block' | 'capped' | 'error'.
    Riusa rotazione pool / cap / extract_contacts come _store_followers_batch.
    """
    sel = pool.next(campaign)
    if sel is None:
        return "capped"
    current_account, current_client = sel
    try:
        user_info = await asyncio.to_thread(current_client.user_info_v1, follower.ig_user_id)
    except Exception as e:
        es = str(e).lower()
        if "protect" in es or "restrict" in es or "community" in es:
            return "soft_block"
        if "429" in es or "too many" in es:
            return "soft_block"
        logger.warning(f"[Bio] user_info @{follower.username} fallito: {e}")
        return "error"
    from app.utils.contact_extract import extract_contacts
    contacts = extract_contacts(user_info)
    await increment_scrape_lookup(db, current_account.id)
    current_account.scrape_lookups_today = (current_account.scrape_lookups_today or 0) + 1
    follower.biography = user_info.biography or None
    follower.is_verified = getattr(user_info, "is_verified", False) or False
    follower.follower_count = getattr(user_info, "follower_count", None)
    follower.following_count = getattr(user_info, "following_count", None)
    ext = getattr(user_info, "external_url", None)
    follower.external_url = contacts.external_url or (str(ext) if ext else None)
    follower.phone = contacts.phone
    follower.email = contacts.email
    follower.whatsapp = contacts.whatsapp
    follower.bio_links = json.dumps(contacts.bio_links) if contacts.bio_links else None
    follower.contact_source = json.dumps(contacts.sources) if contacts.sources else None
    follower.status = FollowerStatus.bio_scraped
    await db.commit()
    await upsert_lead(
        db, ig_user_id=follower.ig_user_id, username=follower.username,
        full_name=follower.full_name, biography=follower.biography,
        contacts=contacts, campaign=campaign, account=current_account,
    )
    logger.info(f"[Bio] @{follower.username} via @{current_account.username} (lookups {current_account.scrape_lookups_today})")
    return "done"
```

(Nota: `_store_followers_batch` resta per il vecchio path interleaved; la duplicazione e' temporanea fino alla deprecazione del path legacy.)

- [ ] **Step 3b: Create scrape_bios.py**

```python
# backend/app/services/scrape_bios.py
"""Fase Bio: estrae bio+contatti dai Follower(status=pending) gia' in lista."""
import asyncio
import random
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import func, select

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.services.bot_state_service import is_halted
from app.services.scraping_pool import ScrapingPool, ScrapingPoolEmpty
from app.services.scraper import fetch_and_store_bio, is_challenge_exception, isolate_challenged_account
from app.utils.events import emit as emit_event
from app.utils.exceptions import BotHaltedError, ScrapeBudgetError, SoftBlockError


def bio_should_continue(target: int | None, done: int) -> bool:
    """True se la Fase Bio deve continuare dato il target e i gia' fatti."""
    if target is None:
        return True
    return done < target


async def scrape_bios(campaign_id: str) -> None:
    """Entry point Fase Bio. Chiamata dal worker."""
    async with AsyncSessionLocal() as db:
        campaign = (await db.execute(select(Campaign).where(Campaign.id == campaign_id))).scalar_one_or_none()
        if not campaign:
            return
        if campaign.status not in (CampaignStatus.scraping, CampaignStatus.scraping_break):
            logger.info(f"[Bio] Stato '{campaign.status.value}' — skip stale retry")
            return
        if await is_halted(db):
            emit_event(campaign_id, "scrape_stopped", "Bot in pausa globale — bio non avviata", level="warn")
            return

        pool = None
        account = None
        done = 0
        consecutive_soft = 0
        try:
            pool = await ScrapingPool.build(db, campaign)
            emit_event(campaign_id, "scrape_start", f"Fase Bio avviata — target {campaign.bio_target or 'tutti i pending'}")
            since_break = 0
            while bio_should_continue(campaign.bio_target, done):
                if await is_halted(db):
                    raise BotHaltedError("kill-switch")
                await db.refresh(campaign)
                if campaign.status not in (CampaignStatus.scraping, CampaignStatus.scraping_break):
                    logger.info(f"[Bio] Stato '{campaign.status.value}' — interrotto a {done}")
                    return
                follower = (await db.execute(
                    select(Follower).where(
                        Follower.campaign_id == campaign_id,
                        Follower.status == FollowerStatus.pending,
                    ).limit(1)
                )).scalar_one_or_none()
                if follower is None:
                    logger.info(f"[Bio] Nessun pending rimasto ({done} fatti)")
                    break
                account = pool.next(campaign)
                account = account[0] if account else None
                outcome = await fetch_and_store_bio(follower, campaign, db, pool)
                if outcome == "capped":
                    campaign.status = CampaignStatus.paused
                    campaign.scrape_outcome = "scrape_capped"
                    campaign.updated_at = datetime.utcnow()
                    await db.commit()
                    emit_event(campaign_id, "scrape_stopped", "Cap giornaliero raggiunto — riprende dopo reset", level="warn")
                    return
                if outcome == "soft_block":
                    consecutive_soft += 1
                    if consecutive_soft >= 3:
                        raise SoftBlockError("3 soft block consecutivi")
                    await asyncio.sleep(random.uniform(90, 180))
                    continue
                if outcome == "done":
                    consecutive_soft = 0
                    done += 1
                    since_break += 1
                    delay = random.uniform(
                        getattr(campaign, "bio_fetch_delay_min", 5.0) or 5.0,
                        getattr(campaign, "bio_fetch_delay_max", 8.0) or 8.0,
                    )
                    await asyncio.sleep(delay)
                # session break
                if since_break >= getattr(campaign, "scrape_session_size", 250):
                    minutes = random.uniform(
                        getattr(campaign, "scrape_break_minutes_min", 30),
                        getattr(campaign, "scrape_break_minutes_max", 45),
                    )
                    campaign.scrape_break_prev_status = CampaignStatus.scraping.value
                    campaign.status = CampaignStatus.scraping_break
                    campaign.scrape_break_until = datetime.utcnow() + timedelta(minutes=minutes)
                    campaign.updated_at = datetime.utcnow()
                    await db.commit()
                    emit_event(campaign_id, "scrape_break", f"Pausa bio {int(minutes)} min dopo {done}")
                    return

            campaign.status = CampaignStatus.ready
            campaign.updated_at = datetime.utcnow()
            await db.commit()
            emit_event(campaign_id, "scrape_complete", f"Fase Bio completata: {done} bio estratte")
        except BotHaltedError:
            campaign.status = CampaignStatus.paused
            await db.commit()
            emit_event(campaign_id, "scrape_stopped", "Bot in pausa globale — bio interrotta", level="warn")
        except SoftBlockError as e:
            campaign.status = CampaignStatus.paused
            await db.commit()
            emit_event(campaign_id, "scrape_stopped", f"Soft block — bio in pausa: {e}", level="error")
        except (ScrapeBudgetError, ScrapingPoolEmpty) as e:
            campaign.status = CampaignStatus.error
            await db.commit()
            emit_event(campaign_id, "scrape_stopped", f"Fase Bio non avviata: {e}", level="error")
        except Exception as e:
            if is_challenge_exception(e) and account is not None:
                await isolate_challenged_account(db, campaign, account, e)
            else:
                logger.error(f"[Bio] Errore {campaign_id}: {e}")
                campaign.status = CampaignStatus.error
                await db.commit()
        finally:
            if pool is not None:
                try:
                    await pool.save_sessions(db)
                except Exception as exc:
                    logger.warning(f"[Bio] save_sessions fallito: {exc}")
                await pool.release()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ./venv/Scripts/python.exe -m pytest tests/test_two_phase_status.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/scrape_bios.py backend/app/services/scraper.py backend/tests/test_two_phase_status.py
git commit -m "feat(scraper): Fase Bio (scrape_bios) + helper fetch_and_store_bio"
```

---

## Task 7: Worker tasks

**Files:**
- Create: `backend/app/workers/list_worker.py`
- Create: `backend/app/workers/bio_worker.py`
- Modify: `backend/app/workers/task_queue.py:1-7` (import) e `:278-285` (functions)

- [ ] **Step 1: Create list_worker.py**

```python
# backend/app/workers/list_worker.py
from loguru import logger
from app.services.scrape_list import list_followers


async def list_followers_task(ctx: dict, campaign_id: str) -> None:
    """ARQ task: Fase Lista (raccolta info base follower)."""
    logger.info(f"[ARQ] list_followers_task started for campaign {campaign_id}")
    try:
        await list_followers(campaign_id)
        logger.info(f"[ARQ] list_followers_task completed for campaign {campaign_id}")
    except Exception as e:
        logger.exception(f"[ARQ] list_followers_task failed for campaign {campaign_id}: {e}")
        raise
```

- [ ] **Step 2: Create bio_worker.py**

```python
# backend/app/workers/bio_worker.py
from loguru import logger
from app.services.scrape_bios import scrape_bios


async def scrape_bios_task(ctx: dict, campaign_id: str) -> None:
    """ARQ task: Fase Bio (estrazione bio/contatti dai pending)."""
    logger.info(f"[ARQ] scrape_bios_task started for campaign {campaign_id}")
    try:
        await scrape_bios(campaign_id)
        logger.info(f"[ARQ] scrape_bios_task completed for campaign {campaign_id}")
    except Exception as e:
        logger.exception(f"[ARQ] scrape_bios_task failed for campaign {campaign_id}: {e}")
        raise
```

- [ ] **Step 3: Register in task_queue.py**

In `backend/app/workers/task_queue.py`, dopo gli altri import worker (riga ~7):

```python
from app.workers.list_worker import list_followers_task
from app.workers.bio_worker import scrape_bios_task
```

In `WorkerSettings.functions` aggiungere i due task:

```python
    functions = [
        scrape_followers_task,
        list_followers_task,
        scrape_bios_task,
        func(run_campaign_task, max_tries=10000),
        pre_generate_messages_task,
        full_batch_generate_task,
        resolve_imports_task,
        qualify_leads_task,
    ]
```

- [ ] **Step 4: Verify import**

Run: `cd backend && ./venv/Scripts/python.exe -c "from app.workers.task_queue import WorkerSettings; print('functions:', len(WorkerSettings.functions))"`
Expected: `functions: 8`

- [ ] **Step 5: Commit**

```bash
git add backend/app/workers/list_worker.py backend/app/workers/bio_worker.py backend/app/workers/task_queue.py
git commit -m "feat(workers): list_followers_task + scrape_bios_task"
```

---

## Task 8: Enqueue helpers + startup-guard

**Files:**
- Modify: `backend/app/services/work_enqueue.py`
- Test: `backend/tests/test_two_phase_enqueue.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_two_phase_enqueue.py
"""Two-phase: enqueue helpers usano job-id dedicati e funzioni giuste."""
import pytest
from app.services.work_enqueue import _enqueue_list_with_redis, _enqueue_bios_with_redis


class _FakeRedis:
    def __init__(self):
        self.enqueued = []

    async def delete(self, *keys):
        pass

    async def enqueue_job(self, fn, *args, **kwargs):
        self.enqueued.append((fn, args, kwargs))


@pytest.mark.asyncio
async def test_enqueue_list():
    r = _FakeRedis()
    await _enqueue_list_with_redis(r, "camp-1")
    fn, args, kwargs = r.enqueued[0]
    assert fn == "list_followers_task"
    assert args == ("camp-1",)
    assert kwargs["_job_id"] == "list:camp-1"


@pytest.mark.asyncio
async def test_enqueue_bios():
    r = _FakeRedis()
    await _enqueue_bios_with_redis(r, "camp-1")
    fn, args, kwargs = r.enqueued[0]
    assert fn == "scrape_bios_task"
    assert args == ("camp-1",)
    assert kwargs["_job_id"] == "bios:camp-1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ./venv/Scripts/python.exe -m pytest tests/test_two_phase_enqueue.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: Add helpers in work_enqueue.py**

Dopo `_enqueue_scrape_with_redis` aggiungere:

```python
async def _enqueue_list_with_redis(redis, campaign_id: str) -> bool:
    job_id = f"list:{campaign_id}"
    await redis.delete(f"arq:job:{job_id}", f"arq:retry:{job_id}", f"arq:in-progress:{job_id}")
    await redis.enqueue_job("list_followers_task", campaign_id, _job_id=job_id, _queue_name=ARQ_MAIN_QUEUE)
    return True


async def _enqueue_bios_with_redis(redis, campaign_id: str) -> bool:
    job_id = f"bios:{campaign_id}"
    await redis.delete(f"arq:job:{job_id}", f"arq:retry:{job_id}", f"arq:in-progress:{job_id}")
    await redis.enqueue_job("scrape_bios_task", campaign_id, _job_id=job_id, _queue_name=ARQ_MAIN_QUEUE)
    return True


async def enqueue_list(campaign_id: str) -> bool:
    import arq
    redis = await arq.create_pool(arq_redis_settings())
    try:
        return await _enqueue_list_with_redis(redis, campaign_id)
    finally:
        await redis.aclose()


async def enqueue_bios(campaign_id: str) -> bool:
    import arq
    redis = await arq.create_pool(arq_redis_settings())
    try:
        return await _enqueue_bios_with_redis(redis, campaign_id)
    finally:
        await redis.aclose()
```

In `pause_active_work_on_startup`, aggiungere i nuovi stati a `active_statuses`:

```python
    active_statuses = (
        CampaignStatus.running,
        CampaignStatus.scraping,
        CampaignStatus.scraping_and_running,
        CampaignStatus.scraping_break,
        CampaignStatus.listing,
        CampaignStatus.listing_break,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ./venv/Scripts/python.exe -m pytest tests/test_two_phase_enqueue.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/work_enqueue.py backend/tests/test_two_phase_enqueue.py
git commit -m "feat(enqueue): enqueue_list/enqueue_bios + startup-guard stati listing"
```

---

## Task 9: Schema response — progress lista/bio

**Files:**
- Modify: `backend/app/schemas/campaign.py:62-104`
- Modify: `backend/app/api/campaigns.py:30-69` (`_enrich_campaign`)
- Test: `backend/tests/test_campaign_progress.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_campaign_progress.py
"""_enrich_campaign deve esporre list_progress e bio_progress dai conteggi follower."""
from app.api.campaigns import compute_phase_progress
from app.models.follower import FollowerStatus


def test_list_progress_counts_all_followers():
    counts = {FollowerStatus.pending: 300, FollowerStatus.bio_scraped: 200}
    lp, bp = compute_phase_progress(counts, list_target=600, bio_target=400)
    assert lp == {"done": 500, "target": 600}
    # bio done = bio_scraped (+ stati a valle), pending+bio = 500
    assert bp == {"done": 200, "target": 400}


def test_progress_targets_none():
    counts = {FollowerStatus.pending: 50}
    lp, bp = compute_phase_progress(counts, list_target=None, bio_target=None)
    assert lp == {"done": 50, "target": None}
    assert bp == {"done": 0, "target": None}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && ./venv/Scripts/python.exe -m pytest tests/test_campaign_progress.py -v`
Expected: FAIL (ImportError `compute_phase_progress`)

- [ ] **Step 3a: Add fields to CampaignResponse**

In `backend/app/schemas/campaign.py`, dentro `CampaignResponse` dopo `scrape_outcome`:

```python
    list_target: int | None = None
    bio_target: int | None = None
    list_progress: dict | None = None
    bio_progress: dict | None = None
```

- [ ] **Step 3b: Add helper + wire in campaigns.py**

In `backend/app/api/campaigns.py`, prima di `_enrich_campaign`:

```python
def compute_phase_progress(counts: dict, list_target: int | None, bio_target: int | None) -> tuple[dict, dict]:
    """Progressi derivati dai conteggi follower per-status.

    Lista done = TUTTI i follower (pending + ogni stato a valle = sono gia' in lista).
    Bio done = follower con bio fatta (bio_scraped + message_generated + pending_approval + sent + replied + failed).
    """
    from app.models.follower import FollowerStatus
    list_done = sum(counts.values())
    bio_done = (
        counts.get(FollowerStatus.bio_scraped, 0)
        + counts.get(FollowerStatus.message_generated, 0)
        + counts.get(FollowerStatus.pending_approval, 0)
        + counts.get(FollowerStatus.sent, 0)
        + counts.get(FollowerStatus.replied, 0)
        + counts.get(FollowerStatus.failed, 0)
    )
    return (
        {"done": list_done, "target": list_target},
        {"done": bio_done, "target": bio_target},
    )
```

In `_enrich_campaign`, nel `model_copy(update={...})` aggiungere:

```python
        "list_progress": compute_phase_progress(counts, campaign.list_target, campaign.bio_target)[0],
        "bio_progress": compute_phase_progress(counts, campaign.list_target, campaign.bio_target)[1],
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && ./venv/Scripts/python.exe -m pytest tests/test_campaign_progress.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/schemas/campaign.py backend/app/api/campaigns.py backend/tests/test_campaign_progress.py
git commit -m "feat(api): list_progress/bio_progress in CampaignResponse"
```

---

## Task 10: Endpoint Fase Lista / Fase Bio

**Files:**
- Modify: `backend/app/api/campaigns.py` (4 endpoint + redirect `start-scrape`)

- [ ] **Step 1: Add list/start + list/stop endpoints**

In `backend/app/api/campaigns.py`, dopo `start_scrape` aggiungere:

```python
from pydantic import BaseModel as _BaseModel

class PhaseStartBody(_BaseModel):
    target: int | None = None


@router.post("/{campaign_id}/list/start", response_model=CampaignResponse)
async def start_list(campaign_id: str, body: PhaseStartBody | None = None, db: AsyncSession = Depends(get_db)):
    campaign = await _get_or_404(campaign_id, db)
    if campaign.source_type == "import":
        raise HTTPException(status_code=400, detail="Le campagne import non usano la Fase Lista (usa la risoluzione).")
    if campaign.status not in (CampaignStatus.draft, CampaignStatus.ready, CampaignStatus.paused, CampaignStatus.error, CampaignStatus.listing_break):
        raise HTTPException(status_code=400, detail="La Fase Lista parte da draft/ready/paused/error/listing_break")
    try:
        await ensure_bot_accepts_work(db)
    except CampaignControlError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not await has_active_role_account(db, campaign_id, ("scraping", "both"), (AccountStatus.active,)):
        raise HTTPException(status_code=400, detail="Nessun account attivo con ruolo scraping o 'entrambi'.")
    if not await _check_redis_reachable():
        raise HTTPException(status_code=503, detail="Redis non raggiungibile.")
    if body and body.target is not None:
        campaign.list_target = body.target
    campaign.status = CampaignStatus.listing
    campaign.updated_at = datetime.utcnow()
    db.add(ActivityLog(campaign_id=campaign.id, action="list_started"))
    await db.commit()
    await db.refresh(campaign)
    from app.services.work_enqueue import enqueue_list
    await enqueue_list(campaign_id)
    return await _enrich_campaign(campaign, db, include_today=True)


@router.post("/{campaign_id}/list/stop", response_model=CampaignResponse)
async def stop_list(campaign_id: str, db: AsyncSession = Depends(get_db)):
    campaign = await _get_or_404(campaign_id, db)
    if campaign.status not in (CampaignStatus.listing, CampaignStatus.listing_break):
        raise HTTPException(status_code=400, detail="La Fase Lista non e' attiva")
    campaign.status = CampaignStatus.paused
    campaign.scrape_break_until = None
    campaign.updated_at = datetime.utcnow()
    db.add(ActivityLog(campaign_id=campaign.id, action="list_stopped"))
    await db.commit()
    return await _enrich_campaign(campaign, db, include_today=True)
```

- [ ] **Step 2: Add bios/start + bios/stop endpoints**

```python
@router.post("/{campaign_id}/bios/start", response_model=CampaignResponse)
async def start_bios(campaign_id: str, body: PhaseStartBody | None = None, db: AsyncSession = Depends(get_db)):
    campaign = await _get_or_404(campaign_id, db)
    if campaign.status not in (CampaignStatus.ready, CampaignStatus.paused, CampaignStatus.error, CampaignStatus.scraping_break):
        raise HTTPException(status_code=400, detail="La Fase Bio parte da ready/paused/error/scraping_break")
    pending = await db.scalar(
        select(func.count(Follower.id)).where(
            Follower.campaign_id == campaign_id,
            Follower.status == FollowerStatus.pending,
        )
    ) or 0
    if pending == 0:
        raise HTTPException(status_code=400, detail="Nessun follower in lista da scrapare. Avvia prima la Fase Lista.")
    try:
        await ensure_bot_accepts_work(db)
    except CampaignControlError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not await has_active_role_account(db, campaign_id, ("scraping", "both"), (AccountStatus.active,)):
        raise HTTPException(status_code=400, detail="Nessun account attivo con ruolo scraping o 'entrambi'.")
    if not await _check_redis_reachable():
        raise HTTPException(status_code=503, detail="Redis non raggiungibile.")
    if body and body.target is not None:
        campaign.bio_target = body.target
    campaign.status = CampaignStatus.scraping
    campaign.updated_at = datetime.utcnow()
    db.add(ActivityLog(campaign_id=campaign.id, action="bios_started"))
    await db.commit()
    await db.refresh(campaign)
    from app.services.work_enqueue import enqueue_bios
    await enqueue_bios(campaign_id)
    return await _enrich_campaign(campaign, db, include_today=True)


@router.post("/{campaign_id}/bios/stop", response_model=CampaignResponse)
async def stop_bios(campaign_id: str, db: AsyncSession = Depends(get_db)):
    campaign = await _get_or_404(campaign_id, db)
    if campaign.status not in (CampaignStatus.scraping, CampaignStatus.scraping_break):
        raise HTTPException(status_code=400, detail="La Fase Bio non e' attiva")
    campaign.status = CampaignStatus.paused
    campaign.scrape_break_until = None
    campaign.updated_at = datetime.utcnow()
    db.add(ActivityLog(campaign_id=campaign.id, action="bios_stopped"))
    await db.commit()
    return await _enrich_campaign(campaign, db, include_today=True)
```

- [ ] **Step 3: Redirect start-scrape legacy → Fase Lista**

In `start_scrape`, per `source_type=scrape` instradare alla Fase Lista invece del vecchio job. Sostituire il blocco enqueue finale:

```python
    try:
        from app.services.work_enqueue import enqueue_resolve, enqueue_list

        if is_import:
            await enqueue_resolve(campaign_id)
        else:
            # source_type=scrape ora usa la Fase Lista (two-phase). Imposta listing.
            campaign.status = CampaignStatus.listing
            await db.commit()
            await enqueue_list(campaign_id)
```

(Mantiene il vecchio `enqueue_scrape`/`scrape_followers_task` registrato per job in volo, ma il nuovo flusso non lo accoda.)

- [ ] **Step 4: Verify import + run full backend test suite**

Run: `cd backend && ./venv/Scripts/python.exe -c "from app.api import campaigns; print('ok')" && ./venv/Scripts/python.exe -m pytest tests/test_two_phase_enqueue.py tests/test_two_phase_status.py tests/test_campaign_progress.py tests/test_list_page_sizing.py tests/test_enqueue_collection.py -v`
Expected: tutti PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/campaigns.py
git commit -m "feat(api): endpoint list/start|stop bios/start|stop + start-scrape -> Fase Lista"
```

---

## Task 11: Frontend types + api

**Files:**
- Modify: `frontend/lib/types.ts:30` (CampaignStatus), Campaign type
- Modify: `frontend/lib/api.ts:126` (campaigns)

- [ ] **Step 1: Update types.ts**

Riga 30 — aggiungere i nuovi stati:

```typescript
export type CampaignStatus = 'draft' | 'listing' | 'listing_break' | 'scraping' | 'scraping_break' | 'scraping_and_running' | 'ready' | 'running' | 'paused' | 'completed' | 'error'
```

Nel type `Campaign` aggiungere i campi (vicino a `scrape_outcome`):

```typescript
  list_target?: number | null
  bio_target?: number | null
  list_progress?: { done: number; target: number | null } | null
  bio_progress?: { done: number; target: number | null } | null
```

- [ ] **Step 2: Update api.ts**

In `frontend/lib/api.ts`, dentro `campaigns: { ... }` dopo `startScrape`:

```typescript
    startList: (id: string, target?: number | null) =>
      request<Campaign>(`/campaigns/${id}/list/start`, { method: 'POST', body: JSON.stringify({ target: target ?? null }) }),
    stopList: (id: string) =>
      request<Campaign>(`/campaigns/${id}/list/stop`, { method: 'POST' }),
    startBios: (id: string, target?: number | null) =>
      request<Campaign>(`/campaigns/${id}/bios/start`, { method: 'POST', body: JSON.stringify({ target: target ?? null }) }),
    stopBios: (id: string) =>
      request<Campaign>(`/campaigns/${id}/bios/stop`, { method: 'POST' }),
```

- [ ] **Step 3: Verify frontend build**

Run: `cd frontend && npm run build`
Expected: build OK, nessun errore TypeScript

- [ ] **Step 4: Commit**

```bash
git add frontend/lib/types.ts frontend/lib/api.ts
git commit -m "feat(fe): types + api per two-phase scraping"
```

---

## Task 12: Frontend — due pannelli Fase Lista / Fase Bio

**Files:**
- Modify: `frontend/app/campaigns/[id]/page.tsx`

- [ ] **Step 1: Add a two-phase control component**

In `frontend/app/campaigns/[id]/page.tsx`, aggiungere un componente che renderizza i due pannelli quando `campaign.source_type === 'scrape'`. Inserirlo nella sezione di controllo scraping (vicino al blocco esistente che usa `api.campaigns.startScrape`, righe ~657-747):

```tsx
function TwoPhasePanel({ campaign, id, action, loadingAction }: {
  campaign: Campaign; id: string; action: (fn: () => Promise<unknown>) => void; loadingAction: boolean;
}) {
  const [listTarget, setListTarget] = useState<string>('')
  const [bioTarget, setBioTarget] = useState<string>('')
  const lp = campaign.list_progress
  const bp = campaign.bio_progress
  const listing = campaign.status === 'listing' || campaign.status === 'listing_break'
  const bioing = campaign.status === 'scraping' || campaign.status === 'scraping_break'
  return (
    <div className="grid gap-4 md:grid-cols-2">
      {/* Fase Lista */}
      <div className="border rounded-lg p-4 space-y-2">
        <div className="font-semibold">Fase 1 — Lista follower</div>
        <div className="text-sm text-muted-foreground">
          Lista: {lp?.done ?? 0}{lp?.target ? ` / ${lp.target}` : ''} {campaign.status === 'listing_break' && '(pausa)'}
        </div>
        <input type="number" placeholder="Target (vuoto = tutta la lista)" value={listTarget}
          onChange={(e) => setListTarget(e.target.value)} className="w-full border rounded px-2 py-1 text-sm" disabled={listing} />
        {!listing ? (
          <button className="btn btn-primary w-full" disabled={loadingAction}
            onClick={() => action(() => api.campaigns.startList(id, listTarget ? Number(listTarget) : null))}>
            Avvia Fase Lista
          </button>
        ) : (
          <button className="btn btn-secondary w-full" disabled={loadingAction}
            onClick={() => action(() => api.campaigns.stopList(id))}>
            Ferma Fase Lista
          </button>
        )}
      </div>
      {/* Fase Bio */}
      <div className="border rounded-lg p-4 space-y-2">
        <div className="font-semibold">Fase 2 — Scraping bio/contatti</div>
        <div className="text-sm text-muted-foreground">
          Bio: {bp?.done ?? 0}{bp?.target ? ` / ${bp.target}` : ''} {campaign.status === 'scraping_break' && '(pausa)'}
        </div>
        <input type="number" placeholder="Target (vuoto = tutti i pending)" value={bioTarget}
          onChange={(e) => setBioTarget(e.target.value)} className="w-full border rounded px-2 py-1 text-sm" disabled={bioing} />
        {!bioing ? (
          <button className="btn btn-primary w-full" disabled={loadingAction}
            onClick={() => action(() => api.campaigns.startBios(id, bioTarget ? Number(bioTarget) : null))}>
            Avvia Fase Bio
          </button>
        ) : (
          <button className="btn btn-secondary w-full" disabled={loadingAction}
            onClick={() => action(() => api.campaigns.stopBios(id))}>
            Ferma Fase Bio
          </button>
        )}
      </div>
    </div>
  )
}
```

(Adattare className ai componenti UI esistenti nel file — usare gli stessi `Button` di shadcn presenti, non `btn` se il file usa `<Button>`.)

- [ ] **Step 2: Render the panel**

Nel render della pagina, per `source_type === 'scrape'`, mostrare `<TwoPhasePanel campaign={campaign} id={id} action={action} loadingAction={loadingAction} />` al posto (o sopra) il vecchio bottone unico `startScrape`. Verificare i nomi reali di `action` / `loadingAction` nel file (righe ~657) e usarli.

- [ ] **Step 3: Verify build**

Run: `cd frontend && npm run build`
Expected: build OK

- [ ] **Step 4: Commit**

```bash
git add frontend/app/campaigns/[id]/page.tsx
git commit -m "feat(fe): pannelli Fase Lista / Fase Bio nel dettaglio campagna"
```

---

## Task 13: Cron recovery — riconoscere stati listing

**Files:**
- Modify: `backend/app/workers/task_queue.py` (`release_stale_locks`, `daily_reset`)
- Modify: `backend/app/services/work_enqueue.py` (`reenqueue_active_work`)

- [ ] **Step 1: Update reenqueue_active_work**

In `backend/app/services/work_enqueue.py`, `reenqueue_active_work`: includere `listing`/`listing_break` tra gli stati che riaccodano la collezione. Nel `select(Campaign).where(Campaign.status.in_(...))` aggiungere `CampaignStatus.listing`. Nel ramo break-restore gestire `listing_break` → `listing`:

```python
                if campaign.status == CampaignStatus.listing_break:
                    campaign.status = CampaignStatus.listing
                    campaign.scrape_break_until = None
                    campaign.scrape_break_prev_status = None
                    campaign.updated_at = datetime.utcnow()
                    counts["breaks_restored"] += 1
```

E nel ramo di enqueue, per `listing` chiamare `_enqueue_list_with_redis`; per `scraping` (Fase Bio) chiamare `_enqueue_bios_with_redis`. (Distinguere: `scraping` ora = Fase Bio; `scraping_and_running` resta legacy scrape+DM.)

- [ ] **Step 2: Update daily_reset restart**

In `task_queue.py` `daily_reset`, dopo il reset, riavviare anche la Fase Bio capped: per campagne in `paused` con `scrape_outcome='scrape_capped'` e follower `pending`, riaccodare `scrape_bios_task`. Aggiungere:

```python
        from app.services.work_enqueue import enqueue_bios
        capped = await db.execute(
            select(Campaign).where(
                Campaign.status == CampaignStatus.paused,
                Campaign.scrape_outcome == "scrape_capped",
            )
        )
        for campaign in capped.scalars().all():
            has_pending = await db.scalar(
                select(func.count(Follower.id)).where(
                    Follower.campaign_id == campaign.id,
                    Follower.status == FollowerStatus.pending,
                )
            )
            if has_pending:
                campaign.status = CampaignStatus.scraping
                campaign.scrape_outcome = None
                await db.commit()
                await enqueue_bios(campaign.id)
```

(Import `Follower`, `FollowerStatus` in cima alla funzione.)

- [ ] **Step 3: Verify imports + run suite**

Run: `cd backend && ./venv/Scripts/python.exe -c "from app.workers.task_queue import WorkerSettings, daily_reset, release_stale_locks; from app.services.work_enqueue import reenqueue_active_work; print('ok')" && ./venv/Scripts/python.exe -m pytest tests/ -q`
Expected: import ok, suite verde (eventuali test che già fallivano prima del lavoro restano invariati — non introdurne di nuovi rossi)

- [ ] **Step 4: Commit**

```bash
git add backend/app/workers/task_queue.py backend/app/services/work_enqueue.py
git commit -m "feat(cron): recovery/reenqueue/daily_reset gestiscono Fase Lista e Bio capped"
```

---

## Task 14: Aggiornare documentazione (regola CLAUDE.md)

**Files:**
- Modify: `CLAUDE.md` (sezione scraping + stati campagna)
- Modify: `docs/project/PROGRESS.md`
- Memory: `C:\Users\39342\.claude\projects\d--BOT-OUTBOUND\memory\project_state.md`

- [ ] **Step 1: Update CLAUDE.md**

Nella sezione stati `campaigns.status` aggiungere `listing`/`listing_break` e la descrizione two-phase. Nella sezione "Scraping avanzato" documentare le due fasi, i nuovi endpoint, `list_target`/`bio_target`, e che `user_followers_v1_chunk` richiede `max_amount` per evitare il burst. Aggiornare la riga del cap (180 → 300 default).

- [ ] **Step 2: Update PROGRESS.md + memory**

Aggiungere voce datata 2026-06-09 con: separazione two-phase scraping implementata, file toccati, comportamento atteso. Aggiornare `project_state.md` in memory con la stessa sintesi.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/project/PROGRESS.md
git commit -m "docs: two-phase scraping (stati listing, endpoint, cap 300)"
```

---

## Self-Review

- **Spec coverage:** Fase Lista (Task 5), Fase Bio (Task 6), stati espliciti (Task 2), migrazione (Task 3), cap 300 (Task 1), endpoint start/stop/target (Task 10), progress (Task 9), worker (Task 7), enqueue+interleaving (Task 8), recovery (Task 13), frontend due pannelli (Task 11-12), challenge handler condiviso (Task 4), import/DM invariati (non toccati). Parallelo fuori scope (non pianificato). ✅
- **Placeholder scan:** ogni step di codice ha codice reale. Le uniche note di adattamento (className UI, nomi `action`/`loadingAction`) richiedono di leggere il file esistente — segnalate esplicitamente. ✅
- **Type consistency:** `next_page_size`, `remaining_for_target`, `bio_should_continue`, `fetch_and_store_bio`, `is_challenge_exception`, `isolate_challenged_account`, `compute_phase_progress`, `enqueue_list`/`enqueue_bios`, job-id `list:`/`bios:` — coerenti tra task. ✅
