# Import Profili da Lista — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Permettere a una campagna di partire da una lista di profili Instagram caricata da file (`.txt`/`.csv`) invece che dallo scraping di follower/following di una pagina target.

**Architecture:** Nuovo `source_type` (`scrape`|`import`) su `campaigns`. Per `import`: il file viene parsato in righe di staging (`imported_profiles`); un worker ARQ dedicato (`resolve_imports_task`) risolve ogni username via `user_info_by_username_v1` (1 call → pk + bio), creando `Follower(bio_scraped)`. Riusa login/anti-detection/session-break dello scraper esistente. Il flusso AI + invio DM a valle resta invariato.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2 async, Alembic, ARQ, instagrapi; frontend Next.js 14 + TS + Tailwind.

**Branch:** `feature/import-profiles` (già creato).

**Spec:** `docs/superpowers/specs/2026-05-29-import-profiles-design.md`

---

## File Structure

**Backend — create:**
- `backend/alembic/versions/013_import_profiles.py` — migrazione: `campaigns.source_type` + tabella `imported_profiles`
- `backend/app/models/imported_profile.py` — modello staging `ImportedProfile`
- `backend/app/utils/ig_username.py` — parser puro URL/username → username normalizzato
- `backend/app/services/import_resolver.py` — parse file → staging + resolve loop (riusa helper scraper)
- `backend/app/workers/import_worker.py` — `resolve_imports_task`
- `backend/tests/test_ig_username.py` — unit parser
- `backend/tests/test_import_resolver.py` — unit classifier esito resolve

**Backend — modify:**
- `backend/app/models/__init__.py` — registra `ImportedProfile`
- `backend/app/models/campaign.py` — campo `source_type`, `target_username` nullable
- `backend/app/schemas/campaign.py` — `source_type` in Create/Response, `target_username` opzionale
- `backend/app/api/campaigns.py` — `create_campaign` (source_type), `start_scrape` (branch import), endpoint `import-profiles` + `import-status`
- `backend/app/services/work_enqueue.py` — `enqueue_resolve`
- `backend/app/workers/task_queue.py` — registra `resolve_imports_task`

**Frontend — modify:**
- `frontend/lib/types.ts` — `source_type`, `ImportStatusResponse`
- `frontend/lib/api.ts` — `importProfiles`, `importStatus`
- `frontend/app/campaigns/new/page.tsx` — toggle sorgente + upload file
- `frontend/app/campaigns/[id]/page.tsx` — pannello contatori import

**Docs — modify (fine):** `CLAUDE.md`, `INDEX.md`, `docs/project/PROGRESS.md`, memory `project_state.md`.

---

## Note di design (lette prima di iniziare)

- **Dedup `global_contacts`**: NON si duplica a resolve-time. Lo username dà `ig_user_id` solo *dopo* la call IG, e il worker DM già fa la dedup a send-time (`campaign_orchestrator.py:412-421`, `skip_reason="already_contacted_globally"`). Mirroriamo lo scraper: il resolve crea sempre il `Follower(bio_scraped)`, la dedup avviene a invio. Coerente e senza logica duplicata.
- **Profilo privato**: si crea comunque il `Follower` (bio eventualmente vuota), coerente con lo scraper.
- **Stati staging** (`imported_profiles.status`): `pending` → `resolved` | `not_found` | `private` | `error`.
- **Riuso scraper**: `app/services/scraper.py` espone già a livello modulo `_get_available_account(db, campaign_id)` e `_get_fallback_account(db, exclude_id, campaign_id)`; `app/utils/instagrapi_client.py` espone `login`, `acquire_scraping_slot`, `release_scraping_slot`, `get_scraping_account_ids`. Il resolver li importa, non li riscrive.
- **`user_info_by_username_v1(username)`** ritorna un oggetto con: `.pk`, `.username`, `.full_name`, `.biography`, `.is_private`, `.is_verified`, `.follower_count`, `.following_count`, `.external_url`, `.profile_pic_url`. `UserNotFound` se inesistente.

---

## Task 1: Migrazione DB — source_type + tabella imported_profiles

**Files:**
- Create: `backend/alembic/versions/013_import_profiles.py`

- [ ] **Step 1: Scrivere la migrazione**

```python
"""Import profiles: source_type on campaigns + imported_profiles staging table.

Revision ID: 013
Revises: 012
Create Date: 2026-05-29
"""
from alembic import op
import sqlalchemy as sa

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column("source_type", sa.String(20), nullable=False, server_default="scrape"),
    )
    # target_username era NOT NULL; per le campagne import non c'è pagina target.
    op.alter_column("campaigns", "target_username", existing_type=sa.String(255), nullable=True)

    op.create_table(
        "imported_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("campaign_id", sa.String(36), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("raw_input", sa.String(512), nullable=False),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("ig_user_id", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("campaign_id", "username", name="uq_import_campaign_username"),
    )
    op.create_index("idx_imported_profiles_campaign", "imported_profiles", ["campaign_id", "status"])


def downgrade() -> None:
    op.drop_index("idx_imported_profiles_campaign", table_name="imported_profiles")
    op.drop_table("imported_profiles")
    op.alter_column("campaigns", "target_username", existing_type=sa.String(255), nullable=False)
    op.drop_column("campaigns", "source_type")
```

- [ ] **Step 2: Applicare la migrazione**

Run: `cd backend && ./venv/Scripts/activate && python -m scripts.migrate`
Expected: stampa `Migrations applied to head.` senza errori.

- [ ] **Step 3: Verificare lo schema**

Run: `cd backend && python -c "import sqlite3; c=sqlite3.connect('data/bot.db'); print([r[1] for r in c.execute('PRAGMA table_info(campaigns)')]); print([r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name='imported_profiles'\")])"`
Expected: la lista colonne campaigns include `source_type`; output include `imported_profiles`.

- [ ] **Step 4: Commit**

```bash
git add backend/alembic/versions/013_import_profiles.py
git commit -m "feat(db): migration 013 — source_type + imported_profiles staging table"
```

---

## Task 2: Modello ImportedProfile

**Files:**
- Create: `backend/app/models/imported_profile.py`
- Modify: `backend/app/models/__init__.py`

- [ ] **Step 1: Scrivere il modello**

```python
import uuid
from datetime import datetime
from sqlalchemy import String, DateTime, BigInteger, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class ImportedProfile(Base):
    """Staging row for an imported Instagram profile awaiting resolution into a Follower."""
    __tablename__ = "imported_profiles"
    __table_args__ = (
        UniqueConstraint("campaign_id", "username", name="uq_import_campaign_username"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    raw_input: Mapped[str] = mapped_column(String(512), nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    # pending | resolved | not_found | private | error
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    ig_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
```

- [ ] **Step 2: Registrare il modello**

In `backend/app/models/__init__.py`, aggiungere l'import dopo `from app.models.bot_state import BotState`:
```python
from app.models.imported_profile import ImportedProfile
```
E aggiungere `"ImportedProfile",` alla lista `__all__`.

- [ ] **Step 3: Verificare l'import**

Run: `cd backend && python -c "from app.models import ImportedProfile; print(ImportedProfile.__tablename__)"`
Expected: stampa `imported_profiles`.

- [ ] **Step 4: Commit**

```bash
git add backend/app/models/imported_profile.py backend/app/models/__init__.py
git commit -m "feat(models): ImportedProfile staging model"
```

---

## Task 3: Campaign.source_type + schemi

**Files:**
- Modify: `backend/app/models/campaign.py`
- Modify: `backend/app/schemas/campaign.py`

- [ ] **Step 1: Aggiungere il campo al modello**

In `backend/app/models/campaign.py`, rendere `target_username` nullable e aggiungere `source_type`. Sostituire la riga:
```python
    target_username: Mapped[str] = mapped_column(String(255), nullable=False)
```
con:
```python
    target_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 'scrape' = scrape follower/following di una pagina; 'import' = lista profili caricata da file
    source_type: Mapped[str] = mapped_column(String(20), nullable=False, default='scrape')
```

- [ ] **Step 2: Aggiornare gli schemi**

In `backend/app/schemas/campaign.py`:

In `CampaignCreate`, rendere `target_username` opzionale e aggiungere `source_type`. Sostituire:
```python
    target_username: str = Field(..., min_length=1, max_length=255)
```
con:
```python
    target_username: str | None = Field(default=None, max_length=255)
    source_type: str = Field(default='scrape', pattern='^(scrape|import)$')
```

In `CampaignResponse`, sostituire:
```python
    target_username: str
```
con:
```python
    target_username: str | None
    source_type: str = 'scrape'
```

- [ ] **Step 3: Validazione coerenza in CampaignCreate**

Aggiungere in `CampaignCreate` (dopo i campi) un validator che impone `target_username` per le campagne scrape:
```python
    from pydantic import model_validator

    @model_validator(mode='after')
    def _check_source(self):
        if self.source_type == 'scrape' and not (self.target_username and self.target_username.strip()):
            raise ValueError("target_username obbligatorio per source_type='scrape'")
        return self
```
(Spostare l'import `model_validator` in cima al file insieme a `from pydantic import BaseModel, Field`.)

- [ ] **Step 4: Aggiornare create_campaign nell'API**

In `backend/app/api/campaigns.py`, dentro `create_campaign`, modificare la costruzione di `Campaign`:
- sostituire `target_username=data.target_username.lstrip("@"),` con:
```python
        target_username=(data.target_username.lstrip("@") if data.target_username else None),
        source_type=data.source_type,
```

- [ ] **Step 5: Verificare**

Run: `cd backend && python -c "from app.schemas.campaign import CampaignCreate; CampaignCreate(name='x', base_message_template='0123456789', source_type='import'); print('import ok'); 
try:
    CampaignCreate(name='x', base_message_template='0123456789', source_type='scrape')
    print('FAIL: should require target')
except Exception as e:
    print('scrape-requires-target ok')"`
Expected: `import ok` e `scrape-requires-target ok`.

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/campaign.py backend/app/schemas/campaign.py backend/app/api/campaigns.py
git commit -m "feat(campaign): source_type field + nullable target_username"
```

---

## Task 4: Parser username (puro, TDD)

**Files:**
- Create: `backend/app/utils/ig_username.py`
- Test: `backend/tests/test_ig_username.py`

- [ ] **Step 1: Scrivere i test (failing)**

```python
from app.utils.ig_username import parse_username, parse_lines


def test_full_url():
    assert parse_username("https://www.instagram.com/john.doe/") == "john.doe"

def test_url_with_query_and_no_scheme():
    assert parse_username("instagram.com/john_doe?hl=it") == "john_doe"

def test_at_handle():
    assert parse_username("@John_Doe") == "john_doe"

def test_bare_username():
    assert parse_username("john.doe") == "john.doe"

def test_csv_first_column():
    assert parse_username("john.doe,Mario Rossi,note") == "john.doe"

def test_invalid_returns_none():
    assert parse_username("not a username!!") is None
    assert parse_username("") is None
    assert parse_username("https://instagram.com/p/ABC123/") is None  # post, non profilo

def test_parse_lines_dedup_and_skip():
    raw = "john.doe\n@john.doe\n\nhttps://instagram.com/jane/\nbad input!!\n"
    result = parse_lines(raw)
    assert result["valid"] == [("john.doe", "john.doe"), ("jane", "https://instagram.com/jane/")]
    assert result["duplicates"] == 1
    assert result["skipped_invalid"] == 1
```

- [ ] **Step 2: Eseguire i test (verificare il fail)**

Run: `cd backend && python -m pytest tests/test_ig_username.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'app.utils.ig_username'`.

- [ ] **Step 3: Implementare il parser**

```python
"""Pure parsing of Instagram profile URLs / usernames from imported file lines."""
import re

_USERNAME_RE = re.compile(r"^[a-z0-9._]{1,30}$")
# Path segments che NON sono username profilo
_RESERVED = {"p", "reel", "reels", "stories", "explore", "tv", "s", "accounts", "direct"}


def parse_username(token: str) -> str | None:
    """Extract a normalized IG username from a URL, @handle, or bare username.
    Returns None if no valid username can be derived."""
    if not token:
        return None
    token = token.strip()
    if not token:
        return None
    # CSV: prendi la prima colonna
    if "," in token:
        token = token.split(",", 1)[0].strip()
    # URL → primo path segment
    if "instagram.com" in token.lower():
        after = re.split(r"instagram\.com/", token, maxsplit=1, flags=re.IGNORECASE)
        if len(after) < 2:
            return None
        path = after[1].split("?", 1)[0].split("#", 1)[0]
        seg = path.strip("/").split("/")[0]
        token = seg
    token = token.lstrip("@").strip().lower()
    if token in _RESERVED:
        return None
    if not _USERNAME_RE.match(token):
        return None
    return token


def parse_lines(raw: str) -> dict:
    """Parse a multi-line blob. Returns valid (list of (username, raw_line)),
    duplicates count, skipped_invalid count. Dedup is case-insensitive on username."""
    valid: list[tuple[str, str]] = []
    seen: set[str] = set()
    duplicates = 0
    skipped_invalid = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        username = parse_username(line)
        if username is None:
            skipped_invalid += 1
            continue
        if username in seen:
            duplicates += 1
            continue
        seen.add(username)
        valid.append((username, line))
    return {"valid": valid, "duplicates": duplicates, "skipped_invalid": skipped_invalid}
```

- [ ] **Step 4: Eseguire i test (verificare il pass)**

Run: `cd backend && python -m pytest tests/test_ig_username.py -v`
Expected: PASS (7 test).

- [ ] **Step 5: Commit**

```bash
git add backend/app/utils/ig_username.py backend/tests/test_ig_username.py
git commit -m "feat(import): pure IG username parser with tests"
```

---

## Task 5: Servizio import — parse file → staging + classifier resolve (TDD)

**Files:**
- Create: `backend/app/services/import_resolver.py`
- Test: `backend/tests/test_import_resolver.py`

Questo task crea il servizio con (a) `store_imported_lines` (parse → righe staging) e (b) `classify_resolution` (funzione pura che mappa l'esito di una call IG → stato staging). Il loop async che usa instagrapi arriva nel Task 6/7 nello stesso file.

- [ ] **Step 1: Scrivere i test del classifier (failing)**

```python
from app.services.import_resolver import classify_resolution
from instagrapi.exceptions import UserNotFound


class _FakeUser:
    def __init__(self, is_private):
        self.is_private = is_private


def test_classify_success_public():
    status, create = classify_resolution(_FakeUser(is_private=False), None)
    assert status == "resolved" and create is True

def test_classify_private_still_creates():
    status, create = classify_resolution(_FakeUser(is_private=True), None)
    assert status == "private" and create is True

def test_classify_not_found():
    status, create = classify_resolution(None, UserNotFound("nope"))
    assert status == "not_found" and create is False

def test_classify_generic_error():
    status, create = classify_resolution(None, ValueError("boom"))
    assert status == "error" and create is False
```

- [ ] **Step 2: Eseguire i test (verificare il fail)**

Run: `cd backend && python -m pytest tests/test_import_resolver.py -v`
Expected: FAIL con `ModuleNotFoundError` o `ImportError`.

- [ ] **Step 3: Implementare servizio (parte sincrona/pura)**

```python
"""Import resolver: turn imported profile lines into bio-scraped Followers.

Reuses the scraper's account selection + instagrapi login. Resolution itself
uses user_info_by_username_v1 (1 call → pk + full bio).
"""
import uuid
from datetime import datetime
from loguru import logger
from sqlalchemy import select
from instagrapi.exceptions import UserNotFound

from app.models.imported_profile import ImportedProfile
from app.utils.ig_username import parse_lines


def classify_resolution(user_info, error) -> tuple[str, bool]:
    """Map an IG resolution outcome → (staging_status, should_create_follower).

    - success public  → ('resolved', True)
    - success private → ('private', True)   # Follower comunque creato
    - UserNotFound    → ('not_found', False)
    - other exception → ('error', False)
    """
    if error is not None:
        if isinstance(error, UserNotFound):
            return "not_found", False
        return "error", False
    if getattr(user_info, "is_private", False):
        return "private", True
    return "resolved", True


async def store_imported_lines(db, campaign_id: str, raw: str) -> dict:
    """Parse a file blob and insert pending ImportedProfile rows.
    Returns counts; raises ValueError if zero valid lines."""
    parsed = parse_lines(raw)
    if not parsed["valid"]:
        raise ValueError("Nessun profilo valido trovato nel file.")

    # Dedup contro righe già presenti per questa campagna
    existing = await db.execute(
        select(ImportedProfile.username).where(ImportedProfile.campaign_id == campaign_id)
    )
    existing_usernames = {r[0] for r in existing.all()}

    inserted = 0
    skipped_existing = 0
    for username, raw_line in parsed["valid"]:
        if username in existing_usernames:
            skipped_existing += 1
            continue
        db.add(ImportedProfile(
            id=str(uuid.uuid4()),
            campaign_id=campaign_id,
            raw_input=raw_line[:512],
            username=username,
            status="pending",
        ))
        inserted += 1
    await db.commit()
    logger.info(f"[Import] Campaign {campaign_id}: {inserted} profili inseriti, "
                f"{parsed['duplicates']} duplicati file, {skipped_existing} già presenti, "
                f"{parsed['skipped_invalid']} righe scartate")
    return {
        "inserted": inserted,
        "duplicates_in_file": parsed["duplicates"],
        "skipped_existing": skipped_existing,
        "skipped_invalid": parsed["skipped_invalid"],
    }
```

- [ ] **Step 4: Eseguire i test (verificare il pass)**

Run: `cd backend && python -m pytest tests/test_import_resolver.py -v`
Expected: PASS (4 test).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/import_resolver.py backend/tests/test_import_resolver.py
git commit -m "feat(import): import_resolver — store_imported_lines + classify_resolution"
```

---

## Task 6: Resolve loop async (riusa anti-detection scraper)

**Files:**
- Modify: `backend/app/services/import_resolver.py`

Aggiungere la funzione `resolve_imports(campaign_id)` che logga un account scraping, itera le righe `pending`, risolve via `user_info_by_username_v1`, crea i `Follower`, gestisce session-break/kill-switch/pausa come lo scraper.

- [ ] **Step 1: Aggiungere import in cima al file**

Aggiungere a `backend/app/services/import_resolver.py` (in testa, dopo gli import esistenti):
```python
import asyncio
import json
import random
from datetime import timedelta

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.models.activity_log import ActivityLog
from app.utils.exceptions import BotHaltedError, ScraperError
from app.utils.instagrapi_client import (
    login as _login, acquire_scraping_slot, release_scraping_slot,
)
from app.services.scraper import _get_available_account, _get_fallback_account
from app.services.bot_state_service import is_halted
from app.utils.events import emit as emit_event
```

- [ ] **Step 2: Aggiungere `_resolve_one` (helper con rotazione 429)**

```python
async def _resolve_one(db, campaign, username, current_client, current_account):
    """Resolve a single username → (user_info | None, error | None, client, account).
    Rotates to a fallback account once on 429/soft-block."""
    for attempt in range(2):
        try:
            info = await asyncio.to_thread(current_client.user_info_by_username_v1, username)
            return info, None, current_client, current_account
        except UserNotFound as e:
            return None, e, current_client, current_account
        except Exception as e:
            es = str(e).lower()
            is_rate = "429" in es or "too many" in es or "rate" in es
            is_soft = "protect" in es or "restrict" in es or "community" in es
            if (is_rate or is_soft) and attempt == 0:
                fb = await _get_fallback_account(db, exclude_id=current_account.id, campaign_id=campaign.id)
                if fb:
                    logger.warning(f"[Import] {'soft-block' if is_soft else '429'} su @{username}; "
                                   f"rotazione @{current_account.username} → @{fb.username}")
                    try:
                        current_client = await _login(fb, db)
                        current_account = fb
                        await asyncio.sleep(random.uniform(30 if is_soft else 15, 60 if is_soft else 30))
                    except Exception as le:
                        logger.warning(f"[Import] fallback login fallito: {le}")
                else:
                    await asyncio.sleep(random.uniform(120, 240) if is_soft else 60)
                continue
            return None, e, current_client, current_account
    return None, RuntimeError("resolve retry esaurito"), current_client, current_account
```

- [ ] **Step 3: Aggiungere `resolve_imports` (entry point del worker)**

```python
async def resolve_imports(campaign_id: str) -> None:
    """Resolve all pending ImportedProfile rows into bio_scraped Followers."""
    _RESOLVING = (CampaignStatus.scraping, CampaignStatus.scraping_and_running, CampaignStatus.scraping_break)
    async with AsyncSessionLocal() as db:
        campaign = (await db.execute(select(Campaign).where(Campaign.id == campaign_id))).scalar_one_or_none()
        if not campaign:
            logger.error(f"[Import] Campaign {campaign_id} not found")
            return
        if campaign.source_type != "import":
            logger.warning(f"[Import] Campaign {campaign_id} non è di tipo import — skip")
            return
        if campaign.status not in _RESOLVING:
            logger.info(f"[Import] Campaign status='{campaign.status.value}' non risolvibile — skip stale retry")
            return
        if await is_halted(db):
            emit_event(campaign_id, "scrape_stopped", "Bot in pausa globale — risoluzione non avviata", level="warn")
            return

        emit_event(campaign_id, "scrape_start", "Risoluzione profili importati avviata...")
        acct_id = None
        try:
            account = await _get_available_account(db, campaign_id=campaign_id)
            await acquire_scraping_slot(account.id)
            acct_id = account.id
            client = await _login(account, db)
            emit_event(campaign_id, "scrape_start", f"Account @{account.username} connesso, risolvo i profili...")

            since_break = 0
            resolved = 0
            while True:
                if await is_halted(db):
                    raise BotHaltedError("global kill-switch active")
                await db.refresh(campaign)
                if campaign.status not in _RESOLVING:
                    logger.info(f"[Import] Interrotto dall'utente dopo {resolved} profili")
                    return

                row = (await db.execute(
                    select(ImportedProfile).where(
                        ImportedProfile.campaign_id == campaign_id,
                        ImportedProfile.status == "pending",
                    ).limit(1)
                )).scalar_one_or_none()
                if row is None:
                    break  # finito

                info, err, client, account = await _resolve_one(db, campaign, row.username, client, account)
                status, create = classify_resolution(info, err)
                row.status = status
                row.error = (str(err)[:255] if err and status == "error" else None)
                if create and info is not None:
                    row.ig_user_id = info.pk
                    dup = (await db.execute(select(Follower).where(
                        Follower.campaign_id == campaign_id, Follower.ig_user_id == info.pk,
                    ))).scalar_one_or_none()
                    if dup is None:
                        ext = getattr(info, "external_url", None)
                        db.add(Follower(
                            campaign_id=campaign_id,
                            ig_user_id=info.pk,
                            username=info.username,
                            full_name=getattr(info, "full_name", None),
                            biography=getattr(info, "biography", None) or None,
                            is_private=getattr(info, "is_private", False),
                            is_verified=getattr(info, "is_verified", False),
                            follower_count=getattr(info, "follower_count", None),
                            following_count=getattr(info, "following_count", None),
                            external_url=str(ext) if ext else None,
                            profile_pic_url=str(info.profile_pic_url) if getattr(info, "profile_pic_url", None) else None,
                            status=FollowerStatus.bio_scraped,
                        ))
                        resolved += 1
                await db.commit()

                since_break += 1
                emit_event(campaign_id, "scrape_batch", f"Risolti {resolved} profili (ultimo: @{row.username} → {status})")

                # delay tra call (riusa bio_fetch_delay)
                dmin = getattr(campaign, "bio_fetch_delay_min", 5.0) or 5.0
                dmax = getattr(campaign, "bio_fetch_delay_max", 8.0) or 8.0
                await asyncio.sleep(random.uniform(dmin, dmax))

                # session break configurabile
                size = getattr(campaign, "scrape_session_size", 250)
                if since_break >= size:
                    bmin = getattr(campaign, "scrape_break_minutes_min", 30)
                    bmax = getattr(campaign, "scrape_break_minutes_max", 45)
                    minutes = random.uniform(bmin, bmax)
                    wake = datetime.utcnow() + timedelta(minutes=minutes)
                    prev = campaign.status.value
                    campaign.scrape_break_prev_status = prev
                    campaign.status = CampaignStatus.scraping_break
                    campaign.scrape_break_until = wake
                    await db.commit()
                    emit_event(campaign_id, "scrape_break", f"Pausa sessione ({int(minutes)} min) dopo {resolved} profili")
                    while datetime.utcnow() < wake:
                        await asyncio.sleep(10)
                        if await is_halted(db):
                            raise BotHaltedError("global kill-switch active")
                        await db.refresh(campaign)
                        if campaign.status != CampaignStatus.scraping_break:
                            break
                    if campaign.status == CampaignStatus.scraping_break:
                        campaign.status = CampaignStatus(prev)
                        campaign.scrape_break_until = None
                        campaign.scrape_break_prev_status = None
                        await db.commit()
                        emit_event(campaign_id, "scrape_resume", "Pausa terminata, risoluzione ripresa")
                    since_break = 0
                    await db.refresh(campaign)
                    if campaign.status not in _RESOLVING:
                        return

            # Completato
            await db.refresh(campaign)
            from sqlalchemy import func as sa_func
            total = await db.scalar(select(sa_func.count(Follower.id)).where(Follower.campaign_id == campaign_id)) or 0
            if campaign.status == CampaignStatus.scraping_and_running:
                campaign.status = CampaignStatus.running
            elif campaign.status in _RESOLVING:
                campaign.status = CampaignStatus.ready
            campaign.total_followers = total
            campaign.messages_pending = total
            campaign.scrape_outcome = "completed"
            campaign.scrape_completed_at = datetime.utcnow()
            campaign.updated_at = datetime.utcnow()
            db.add(ActivityLog(campaign_id=campaign_id, action="import_resolved",
                               details=json.dumps({"resolved": resolved, "total": total})))
            await db.commit()
            emit_event(campaign_id, "scrape_complete", f"Risoluzione completata: {total} profili pronti.")

        except BotHaltedError:
            await db.refresh(campaign)
            campaign.scrape_outcome = "partial"
            campaign.updated_at = datetime.utcnow()
            await db.commit()
            emit_event(campaign_id, "scrape_stopped", "Bot in pausa globale — risoluzione interrotta", level="warn")
        except ScraperError as e:
            logger.error(f"[Import] {e}")
            campaign.status = CampaignStatus.error
            await db.commit()
            emit_event(campaign_id, "scrape_stopped", str(e), level="error")
        except Exception as e:
            logger.exception(f"[Import] resolve failed for {campaign_id}: {e}")
            campaign.status = CampaignStatus.error
            await db.commit()
            emit_event(campaign_id, "scrape_stopped", f"Errore risoluzione: {str(e)[:120]}", level="error")
        finally:
            if acct_id:
                await release_scraping_slot(acct_id)
```

- [ ] **Step 4: Verificare import del modulo**

Run: `cd backend && python -c "from app.services.import_resolver import resolve_imports, store_imported_lines, classify_resolution; print('ok')"`
Expected: stampa `ok` (nessun ImportError/circular import).

- [ ] **Step 5: Rieseguire i test del classifier (non regrediti)**

Run: `cd backend && python -m pytest tests/test_import_resolver.py -v`
Expected: PASS (4 test).

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/import_resolver.py
git commit -m "feat(import): resolve_imports async loop with anti-detection reuse"
```

---

## Task 7: Worker ARQ + enqueue

**Files:**
- Create: `backend/app/workers/import_worker.py`
- Modify: `backend/app/workers/task_queue.py`
- Modify: `backend/app/services/work_enqueue.py`

- [ ] **Step 1: Scrivere il worker**

`backend/app/workers/import_worker.py`:
```python
from loguru import logger
from app.services.import_resolver import resolve_imports


async def resolve_imports_task(ctx: dict, campaign_id: str) -> None:
    """ARQ task: resolve imported profiles into bio_scraped Followers."""
    logger.info(f"[ARQ] resolve_imports_task started for campaign {campaign_id}")
    try:
        await resolve_imports(campaign_id)
        logger.info(f"[ARQ] resolve_imports_task completed for campaign {campaign_id}")
    except Exception as e:
        logger.exception(f"[ARQ] resolve_imports_task failed for {campaign_id}: {e}")
        raise
```

- [ ] **Step 2: Registrare il task in WorkerSettings**

In `backend/app/workers/task_queue.py`:
- Aggiungere l'import in cima: `from app.workers.import_worker import resolve_imports_task`
- In `class WorkerSettings`, aggiungere `resolve_imports_task,` alla lista `functions`.

- [ ] **Step 3: Aggiungere enqueue_resolve**

Aprire `backend/app/services/work_enqueue.py` e individuare la funzione `enqueue_scrape` esistente (usa `_job_id=f"scrape:{campaign_id}"`). Subito sotto, aggiungere una funzione gemella:
```python
async def enqueue_resolve(campaign_id: str) -> None:
    """Enqueue the import-resolution job (dedup by job id, mirrors enqueue_scrape)."""
    redis = await _create_pool()
    try:
        await redis.enqueue_job(
            "resolve_imports_task", campaign_id, _job_id=f"resolve:{campaign_id}"
        )
    finally:
        await redis.aclose()
```
> NB: replicare esattamente lo stile di `enqueue_scrape` di questo file (nome del pool/helper, gestione `aclose`/`close`). Se `enqueue_scrape` usa `arq.create_pool(arq_redis_settings())` inline invece di `_create_pool()`, usare la stessa forma qui.

- [ ] **Step 4: Aggiungere pulizia chiave ARQ resolve in delete_campaign**

In `backend/app/api/campaigns.py`, dentro `delete_campaign`, nella lista dei suffissi puliti:
```python
        for suffix in [f"scrape:{campaign_id}", f"pregen:{campaign_id}:preview", f"pregen:{campaign_id}:full"]:
```
aggiungere `f"resolve:{campaign_id}"`:
```python
        for suffix in [f"scrape:{campaign_id}", f"resolve:{campaign_id}", f"pregen:{campaign_id}:preview", f"pregen:{campaign_id}:full"]:
```

- [ ] **Step 5: Verificare import**

Run: `cd backend && python -c "from app.workers.task_queue import WorkerSettings; print([f.__name__ if hasattr(f,'__name__') else getattr(f,'coroutine',f).__name__ for f in WorkerSettings.functions])"`
Expected: la lista include `resolve_imports_task`.

- [ ] **Step 6: Commit**

```bash
git add backend/app/workers/import_worker.py backend/app/workers/task_queue.py backend/app/services/work_enqueue.py backend/app/api/campaigns.py
git commit -m "feat(import): resolve_imports_task worker + enqueue_resolve"
```

---

## Task 8: Endpoint upload + branch start-scrape

**Files:**
- Modify: `backend/app/api/campaigns.py`

- [ ] **Step 1: Aggiungere import UploadFile**

In cima a `backend/app/api/campaigns.py`, estendere l'import FastAPI esistente con `UploadFile, File`:
```python
from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks, UploadFile, File
```
E aggiungere:
```python
from app.models.imported_profile import ImportedProfile
from app.services.import_resolver import store_imported_lines
```

- [ ] **Step 2: Endpoint di upload**

Aggiungere (es. dopo `create_campaign`):
```python
@router.post("/{campaign_id}/import-profiles")
async def import_profiles(campaign_id: str, file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    """Upload a .txt/.csv of profile URLs/usernames into the import staging table."""
    campaign = await _get_or_404(campaign_id, db)
    if campaign.source_type != "import":
        raise HTTPException(status_code=400, detail="La campagna non è di tipo 'import'")
    if campaign.status != CampaignStatus.draft:
        raise HTTPException(status_code=400, detail="I profili si caricano solo in stato draft")
    if not (file.filename or "").lower().endswith((".txt", ".csv")):
        raise HTTPException(status_code=400, detail="Formato file non supportato (usa .txt o .csv)")
    raw_bytes = await file.read()
    if len(raw_bytes) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File troppo grande (max 5MB)")
    try:
        raw = raw_bytes.decode("utf-8", errors="ignore")
    except Exception:
        raise HTTPException(status_code=400, detail="Impossibile leggere il file (encoding)")
    try:
        counts = await store_imported_lines(db, campaign_id, raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return counts
```

- [ ] **Step 3: Endpoint stato import (contatori staging)**

```python
@router.get("/{campaign_id}/import-status")
async def import_status(campaign_id: str, db: AsyncSession = Depends(get_db)):
    """Per-status counts of imported_profiles for the import panel."""
    await _get_or_404(campaign_id, db)
    rows = await db.execute(
        select(ImportedProfile.status, func.count(ImportedProfile.id))
        .where(ImportedProfile.campaign_id == campaign_id)
        .group_by(ImportedProfile.status)
    )
    counts = {s: c for s, c in rows.all()}
    total = sum(counts.values())
    return {
        "total": total,
        "pending": counts.get("pending", 0),
        "resolved": counts.get("resolved", 0),
        "not_found": counts.get("not_found", 0),
        "private": counts.get("private", 0),
        "error": counts.get("error", 0),
    }
```

- [ ] **Step 4: Branch import in start_scrape**

In `start_scrape`, dopo il check `if campaign.status not in (CampaignStatus.draft,)` e prima del check account, aggiungere il ramo import. Sostituire il blocco da `if not await has_active_role_account(...)` fino a `await enqueue_scrape(campaign_id)` con una versione che dirama. In pratica, subito dopo `await ensure_bot_accepts_work(db)`:
```python
    is_import = campaign.source_type == "import"
    if is_import:
        pending = await db.scalar(
            select(func.count(ImportedProfile.id)).where(
                ImportedProfile.campaign_id == campaign_id,
                ImportedProfile.status == "pending",
            )
        ) or 0
        if pending == 0:
            raise HTTPException(status_code=400, detail="Nessun profilo importato da risolvere. Carica un file prima di avviare.")
```
Poi il check account `has_active_role_account(... ("scraping","both") ...)` resta valido per entrambi i casi (anche import serve un account scraping per le call). Infine, sostituire l'enqueue:
```python
        from app.services.work_enqueue import enqueue_scrape, enqueue_resolve
        if is_import:
            await enqueue_resolve(campaign_id)
        else:
            await enqueue_scrape(campaign_id)
```
(Nel blocco `except` di rollback, lo stato torna a `draft` come già fa per lo scrape — invariato.)

- [ ] **Step 5: Verificare boot API**

Run: `cd backend && python -c "from app.api.campaigns import router; print('routes', len(router.routes))"`
Expected: stampa il numero di route senza errori di import.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/campaigns.py
git commit -m "feat(import): upload endpoint, import-status, start-scrape import branch"
```

---

## Task 9: Frontend — types + api

**Files:**
- Modify: `frontend/lib/types.ts`
- Modify: `frontend/lib/api.ts`

- [ ] **Step 1: Aggiornare i types**

In `frontend/lib/types.ts`:
- Nell'interfaccia `Campaign`, dopo `scrape_mode: 'followers' | 'following'` aggiungere:
```typescript
  source_type: 'scrape' | 'import'
```
- Rendere `target_username` nullable: cambiare `target_username: string` in `target_username: string | null` (sia in `Campaign`).
- In `CampaignCreate`, cambiare `target_username: string` in `target_username?: string | null` e aggiungere `source_type?: 'scrape' | 'import'`.
- Aggiungere in fondo al file:
```typescript
export interface ImportStatusResponse {
  total: number
  pending: number
  resolved: number
  not_found: number
  private: number
  error: number
}
export interface ImportUploadResponse {
  inserted: number
  duplicates_in_file: number
  skipped_existing: number
  skipped_invalid: number
}
```

- [ ] **Step 2: Aggiungere i metodi API**

In `frontend/lib/api.ts`, importare i nuovi tipi nell'`import type { ... }` in cima (aggiungere `ImportStatusResponse, ImportUploadResponse`). Poi dentro `campaigns: { ... }`, aggiungere:
```typescript
    importProfiles: async (id: string, file: File): Promise<ImportUploadResponse> => {
      const token = getAuthToken()
      const fd = new FormData()
      fd.append('file', file)
      const res = await fetch(`${BASE_URL}/campaigns/${id}/import-profiles`, {
        method: 'POST',
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        body: fd,
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }))
        throw new Error(err.detail || `HTTP ${res.status}`)
      }
      return res.json()
    },
    importStatus: (id: string) => request<ImportStatusResponse>(`/campaigns/${id}/import-status`),
```
> NB: l'upload usa `fetch` diretto (non `request`) perché `request` forza `Content-Type: application/json`, incompatibile con `FormData` (il browser deve impostare il boundary multipart).

- [ ] **Step 3: Verificare il typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: nessun errore introdotto dai nuovi tipi (eventuali errori preesistenti non correlati non bloccano).

- [ ] **Step 4: Commit**

```bash
git add frontend/lib/types.ts frontend/lib/api.ts
git commit -m "feat(import): frontend types + import API methods"
```

---

## Task 10: Frontend — form nuova campagna (toggle sorgente + upload)

**Files:**
- Modify: `frontend/app/campaigns/new/page.tsx`

> Leggere prima `frontend/AGENTS.md`: la versione Next.js può differire dal training; consultare `node_modules/next/dist/docs/` se serve.

- [ ] **Step 1: Stato sorgente + file nel componente**

Dopo `const [loading, setLoading] = useState(false)` aggiungere:
```tsx
  const [sourceType, setSourceType] = useState<'scrape' | 'import'>('scrape')
  const [importFile, setImportFile] = useState<File | null>(null)
```

- [ ] **Step 2: Validazione condizionale**

In `validate()`, rendere `target_username` obbligatorio solo per scrape e il file per import. Sostituire il blocco username con:
```tsx
    if (sourceType === 'scrape') {
      const username = form.target_username.replace(/^@/, '').trim()
      if (!username) {
        errs.target_username = "L'username è obbligatorio"
      } else if (!IG_USERNAME_RE.test(username)) {
        errs.target_username = 'Username non valido. Solo lettere, numeri, punti e underscore (max 30 caratteri)'
      }
    } else if (!importFile) {
      errs.import_file = 'Carica un file con i profili da contattare'
    }
```

- [ ] **Step 3: Submit ramificato**

Sostituire il corpo di `handleSubmit` (la parte dentro `try`) con:
```tsx
      const campaign = await api.campaigns.create({
        name: form.name.trim(),
        source_type: sourceType,
        target_username: sourceType === 'scrape' ? form.target_username.replace(/^@/, '').trim() : null,
        scrape_mode: form.scrape_mode,
        base_message_template: form.base_message_template,
        message_template_b: showTemplateB && form.message_template_b ? form.message_template_b : null,
        ai_prompt_context: form.ai_prompt_context || undefined,
        daily_limit: form.daily_limit ? Number(form.daily_limit) : null,
        require_approval: form.require_approval,
        approval_sample_size: form.approval_sample_size ? Number(form.approval_sample_size) : 5,
        scrape_session_size: Number(advancedConfig.scrape_session_size) || 250,
        scrape_break_minutes_min: Number(advancedConfig.scrape_break_minutes_min) || 30,
        scrape_break_minutes_max: Number(advancedConfig.scrape_break_minutes_max) || 45,
        bio_fetch_delay_min: Number(advancedConfig.bio_fetch_delay_min) || 5,
        bio_fetch_delay_max: Number(advancedConfig.bio_fetch_delay_max) || 8,
      })
      if (sourceType === 'import' && importFile) {
        const res = await api.campaigns.importProfiles(campaign.id, importFile)
        toast.success(`Campagna creata! ${res.inserted} profili importati`)
      } else {
        toast.success('Campagna creata!')
      }
      router.push(`/campaigns/${campaign.id}`)
```

- [ ] **Step 4: Toggle sorgente in cima alla Card**

Subito dentro `<CardContent ...>`, PRIMA del campo "Nome campagna", inserire:
```tsx
            <div className="space-y-2">
              <label className="text-sm text-gray-300 font-medium">Sorgente profili</label>
              <div className="flex gap-3">
                <button type="button" onClick={() => setSourceType('scrape')}
                  className={`flex-1 py-2 px-3 rounded-lg border text-sm font-medium transition-colors ${sourceType === 'scrape' ? 'bg-purple-600/20 border-purple-500 text-purple-300' : 'bg-gray-800 border-gray-700 text-gray-400 hover:border-gray-600'}`}>
                  Scraping pagina
                  <span className="block text-xs font-normal mt-0.5 opacity-70">Follower/following di una pagina target</span>
                </button>
                <button type="button" onClick={() => setSourceType('import')}
                  className={`flex-1 py-2 px-3 rounded-lg border text-sm font-medium transition-colors ${sourceType === 'import' ? 'bg-purple-600/20 border-purple-500 text-purple-300' : 'bg-gray-800 border-gray-700 text-gray-400 hover:border-gray-600'}`}>
                  Lista importata
                  <span className="block text-xs font-normal mt-0.5 opacity-70">File di URL/username</span>
                </button>
              </div>
            </div>
```

- [ ] **Step 5: Rendere condizionali i campi scrape + aggiungere upload**

Avvolgere il blocco "Pagina target (username)" e il blocco "Modalità raccolta profili" in `{sourceType === 'scrape' && ( ... )}`. Subito dopo, aggiungere il blocco upload per import:
```tsx
            {sourceType === 'import' && (
              <div className="space-y-1.5">
                <label className="text-sm text-gray-300 font-medium">File profili (.txt / .csv) *</label>
                <Input type="file" accept=".txt,.csv"
                  onChange={e => { setImportFile(e.target.files?.[0] ?? null); setErrors(er => ({ ...er, import_file: '' })) }}
                  className={`bg-gray-800 border-gray-700 text-white ${errors.import_file ? 'border-red-600' : ''}`} />
                {errors.import_file
                  ? <p className="text-xs text-red-400">{errors.import_file}</p>
                  : <p className="text-xs text-gray-500">Un profilo per riga (URL Instagram o username). Serve un account con ruolo scraping/both assegnato per recuperare le bio.</p>}
              </div>
            )}
```

- [ ] **Step 6: Verificare build/lint**

Run: `cd frontend && npx tsc --noEmit && npm run lint`
Expected: nessun errore nuovo nel file modificato.

- [ ] **Step 7: Commit**

```bash
git add frontend/app/campaigns/new/page.tsx
git commit -m "feat(import): campaign form source toggle + file upload"
```

---

## Task 11: Frontend — pannello stato import nel dettaglio

**Files:**
- Modify: `frontend/app/campaigns/[id]/page.tsx`

- [ ] **Step 1: Individuare il punto di inserimento**

Aprire `frontend/app/campaigns/[id]/page.tsx`, individuare dove la campagna (`campaign`) è disponibile e dove vengono mostrati i pannelli di stato/scraping. Identificare l'hook SWR usato (es. `useSWR`) per replicarne lo stile.

- [ ] **Step 2: Aggiungere il fetch dello stato import**

Vicino agli altri hook di data fetching, aggiungere (adattando al pattern SWR del file):
```tsx
  const { data: importStatus } = useSWR(
    campaign?.source_type === 'import' ? `/campaigns/${campaignId}/import-status` : null,
    () => api.campaigns.importStatus(campaignId),
    { refreshInterval: 5000 }
  )
```
> `campaignId` è la prop/route param già usata nel file (adattare al nome reale, es. `id` o `params.id`). Importare `api` se non già importato.

- [ ] **Step 3: Renderizzare il pannello**

Dove si mostrano i contatori della campagna, aggiungere (solo per import):
```tsx
      {campaign?.source_type === 'import' && importStatus && (
        <div className="rounded-lg border border-gray-800 bg-gray-900 p-4 space-y-2">
          <h3 className="text-sm font-medium text-gray-200">Profili importati</h3>
          <div className="grid grid-cols-3 gap-2 text-sm">
            <div><span className="text-gray-400">Totale:</span> <span className="text-white">{importStatus.total}</span></div>
            <div><span className="text-gray-400">Da risolvere:</span> <span className="text-yellow-300">{importStatus.pending}</span></div>
            <div><span className="text-gray-400">Risolti:</span> <span className="text-green-400">{importStatus.resolved}</span></div>
            <div><span className="text-gray-400">Non trovati:</span> <span className="text-gray-300">{importStatus.not_found}</span></div>
            <div><span className="text-gray-400">Privati:</span> <span className="text-gray-300">{importStatus.private}</span></div>
            <div><span className="text-gray-400">Errori:</span> <span className="text-red-400">{importStatus.error}</span></div>
          </div>
        </div>
      )}
```

- [ ] **Step 4: Etichetta stato "Risoluzione"**

Se il file mappa `CampaignStatus` → label leggibili (es. `scraping` → "Scraping"), aggiungere una variante: quando `campaign.source_type === 'import'` e lo stato è `scraping`/`scraping_break`, mostrare "Risoluzione profili" / "Pausa risoluzione". Individuare la map/funzione esistente e aggiungere il branch condizionale su `source_type`.

- [ ] **Step 5: Verificare build/lint**

Run: `cd frontend && npx tsc --noEmit && npm run lint`
Expected: nessun errore nuovo.

- [ ] **Step 6: Commit**

```bash
git add frontend/app/campaigns/[id]/page.tsx
git commit -m "feat(import): import status panel on campaign detail"
```

---

## Task 12: Verifica end-to-end manuale + docs/memory

**Files:**
- Modify: `CLAUDE.md`, `INDEX.md`, `docs/project/PROGRESS.md`
- Modify: `C:\Users\39342\.claude\projects\d--BOT-OUTBOUND\memory\project_state.md`

- [ ] **Step 1: Smoke test backend**

Avviare Redis + API + worker (vedi CLAUDE.md sezione avvio). Creare via UI una campagna `Lista importata`, caricare un `.txt` con 3 username noti (di cui uno inesistente), assegnare un account scraping/both, premere avvio.
Expected: stato passa a "Risoluzione", il pannello import mostra `resolved` crescere e `not_found=1`; a fine, stato `ready` e i Follower compaiono in `bio_scraped`.

- [ ] **Step 2: Verificare il flusso DM invariato**

Generare messaggi (pre-gen o auto) e avviare i DM su questa campagna.
Expected: i messaggi si generano dai `Follower` `bio_scraped` esattamente come per le campagne scrape; nessuna regressione.

- [ ] **Step 3: Aggiornare la documentazione di progetto**

- `CLAUDE.md`: in "Database schema" → `campaigns`, documentare `source_type`; aggiungere la voce tabella `imported_profiles`. In "Scala e parallelismo" o una nuova nota, descrivere la modalità import.
- `INDEX.md`: aggiungere la feature import allo stato globale.
- `docs/project/PROGRESS.md`: nuova voce datata 2026-05-29 con la feature import (file toccati, comportamento).

- [ ] **Step 4: Aggiornare la memoria persistente**

In `C:\Users\39342\.claude\projects\d--BOT-OUTBOUND\memory\project_state.md` aggiungere sezione datata 2026-05-29: feature import profili (source_type, imported_profiles, resolve worker, endpoint, UI), branch `feature/import-profiles`. Verificare che `MEMORY.md` resti coerente.

- [ ] **Step 5: Eseguire l'intera suite di test backend**

Run: `cd backend && python -m pytest tests/ -q`
Expected: i nuovi test passano; nessuna regressione rispetto al baseline.

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md INDEX.md docs/project/PROGRESS.md
git commit -m "docs: document import-profiles feature"
```

---

## Self-Review (eseguita)

**1. Spec coverage:**
- `source_type` su campaigns → Task 1, 3. ✅
- Tabella `imported_profiles` → Task 1, 2. ✅
- Endpoint upload `.txt/.csv` → Task 8. ✅
- Parser URL/username, prima colonna CSV → Task 4. ✅
- Resolve via `user_info_by_username_v1` + bio → Task 6. ✅
- Riuso anti-detection (login, rotazione 429, session-break, kill-switch) → Task 6. ✅
- Profilo privato crea Follower → Task 5 (classifier) + Task 6. ✅
- Stati staging pending/resolved/not_found/private/error → Task 2, 5, 6. ✅
- Account scraping/both richiesto → Task 8 (check riusato). ✅
- DM downstream invariato → nessuna modifica al flusso AI/invio (verificato in Task 12 step 2). ✅
- Frontend toggle + upload + pannello contatori → Task 9, 10, 11. ✅
- Migrazione default `scrape` su campagne esistenti → Task 1 (`server_default="scrape"`). ✅
- Crash recovery idempotente (riprende solo `pending`) → Task 6 (loop su `status='pending'`). ✅

**2. Placeholder scan:** nessun TODO/TBD; ogni step di codice ha codice reale. I due punti "adattare al pattern del file" (Task 7 step 3 enqueue, Task 11 SWR/label) sono istruzioni esplicite di allineamento a codice esistente, non placeholder di logica.

**3. Type consistency:** `source_type: 'scrape'|'import'`, stati staging `pending|resolved|not_found|private|error`, `classify_resolution → (status, create)`, `store_imported_lines → {inserted, duplicates_in_file, skipped_existing, skipped_invalid}`, `import-status → {total,pending,resolved,not_found,private,error}` coerenti tra backend (Task 5/6/8) e frontend (Task 9/11). `resolve_imports_task` / `enqueue_resolve` / job id `resolve:{campaign_id}` coerenti (Task 7).

**Divergenza nota dallo spec:** la dedup `global_contacts` non è duplicata a resolve-time (ig_user_id noto solo post-call; la dedup a send-time del worker DM la copre già). Comportamento identico per l'utente, meno codice. Documentato nelle "Note di design".
