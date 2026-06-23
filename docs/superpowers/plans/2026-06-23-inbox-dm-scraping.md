# Inbox DM Scraping (`scrape_mode = dm_threads`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Aggiungere una modalità di scraping che raccoglie i contatti dalle conversazioni DM già esistenti di **un** account (non dai follower/following di un target), con engine selezionabile per campagna (`browser` prudente di default / `api` veloce), riusando la macchina two-phase (Fase Lista → Fase Bio) esistente.

**Architecture:** Una nuova Fase Lista alternativa (`scrape_inbox.run_inbox_list`) si innesta nello stesso ARQ status-flow `listing`/`listing_break` già usato da `scrape_list.list_followers`, via un branch su `scrape_mode == 'dm_threads'`. La sorgente dei contatti è astratta dietro `InboxListSource` con due implementazioni intercambiabili (`ApiInboxSource`, `BrowserInboxSource`). La Fase Bio (`scrape_bios.py`) resta invariata e processa i `Follower(status=pending)` prodotti.

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy async / ARQ (Redis) / instagrapi (private API) / Patchright (browser) / Alembic / pytest. Frontend Next.js (app router) + `frontend/lib/api.ts`.

## Global Constraints

- **Spec di riferimento:** `docs/superpowers/specs/2026-06-23-inbox-dm-scraping-design.md` — ogni task ne implementa una parte.
- **Vincolo 1-account (hard-coded, 3 livelli):** una campagna `dm_threads` deve avere **esattamente 1** account assegnato attivo in fase lista. Validato a start, in assegnazione account (HTTP 400), e in UI.
- **Perimetro thread:** solo 1-a-1 (gruppi scartati: `len(other_users) != 1`), **entrambe le direzioni** (inbound + outbound). Pending/richieste **esclusi** in Fase 1.
- **Resume-by-frontier:** il cursore/marker in `Campaign.scrape_cursor` è solo ottimizzazione intra-engine; la correttezza del riavvio (e dello switch engine) è garantita dal **dedup** sui `Follower` già salvati (`campaign_id` + `ig_user_id`), mai duplicati.
- **`Follower` campi reali (NON inventare):** `campaign_id`, `ig_user_id` (int, = `int(user.pk)`), `username`, `full_name`, `is_private`, `is_verified`, `profile_pic_url` (str|None), `status=FollowerStatus.pending`.
- **Fuori scope Fase 1:** messaggio re-engage, lettura ultimo messaggio thread, filtro anti-spam (`status==replied`), pending inbox, bio via browser, switch engine automatico.
- **Default engine:** `browser`. **Bio resta su API** (rischio documentato, accettato).
- **Pattern obbligati dal codice esistente:** pausa sessione via `Retry(defer=...)` ritornando i secondi dal job (mai `sleep` lungo in-job); challenge IG via `isolate_challenged_account`; stato fase = `listing` / `listing_break`; job id ARQ `list:{campaign_id}` (riusato, nessun nuovo job id).

---

### Task 1: Settings `INBOX_*` per il pacing browser/api

**Files:**
- Modify: `backend/app/config.py` (classe `Settings`, dopo la riga 96 `list_long_pause_max_seconds`)
- Test: `backend/tests/test_inbox_settings.py`

**Interfaces:**
- Produces: `settings.inbox_api_page_delay_min_seconds`, `settings.inbox_api_page_delay_max_seconds`, `settings.inbox_browser_scroll_min_seconds`, `settings.inbox_browser_scroll_max_seconds`, `settings.inbox_browser_micropause_every_min`, `settings.inbox_browser_micropause_every_max`, `settings.inbox_browser_micropause_min_seconds`, `settings.inbox_browser_micropause_max_seconds`, `settings.inbox_browser_feedbrowse_probability`, `settings.inbox_browser_feedbrowse_min_seconds`, `settings.inbox_browser_feedbrowse_max_seconds`, `settings.inbox_session_size`, `settings.inbox_break_min_minutes`, `settings.inbox_break_max_minutes` — tutti letti da `.env` con default cauti.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_inbox_settings.py
"""Inbox scraping: i settings di pacing devono esistere con bound cauti."""
from app.config import settings


def test_inbox_api_pacing_present():
    assert settings.inbox_api_page_delay_min_seconds <= settings.inbox_api_page_delay_max_seconds
    assert settings.inbox_api_page_delay_min_seconds >= 1


def test_inbox_browser_pacing_present():
    assert 2 <= settings.inbox_browser_scroll_min_seconds <= settings.inbox_browser_scroll_max_seconds
    assert settings.inbox_browser_micropause_every_min <= settings.inbox_browser_micropause_every_max
    assert settings.inbox_browser_micropause_min_seconds <= settings.inbox_browser_micropause_max_seconds
    assert 0.0 <= settings.inbox_browser_feedbrowse_probability <= 1.0
    assert settings.inbox_browser_feedbrowse_min_seconds <= settings.inbox_browser_feedbrowse_max_seconds


def test_inbox_session_and_break_bounds():
    assert settings.inbox_session_size >= 10
    assert settings.inbox_break_min_minutes <= settings.inbox_break_max_minutes
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_inbox_settings.py -v`
Expected: FAIL con `AttributeError: 'Settings' object has no attribute 'inbox_api_page_delay_min_seconds'`

- [ ] **Step 3: Add the settings**

In `backend/app/config.py`, subito dopo la riga `list_long_pause_max_seconds: int = 60` (riga 96), aggiungi:

```python
    # ── Inbox DM scraping (scrape_mode=dm_threads) ─────────────────────────
    # Engine API (account secondari): pacing tra pagine direct_v2/inbox.
    inbox_api_page_delay_min_seconds: int = 5
    inbox_api_page_delay_max_seconds: int = 12
    # Engine browser (account principali): scroll umano con micro-pause.
    inbox_browser_scroll_min_seconds: float = 2.0
    inbox_browser_scroll_max_seconds: float = 6.0
    # Micro-pausa "distrazione" in-place ogni N step di scroll.
    inbox_browser_micropause_every_min: int = 8
    inbox_browser_micropause_every_max: int = 15
    inbox_browser_micropause_min_seconds: int = 5
    inbox_browser_micropause_max_seconds: int = 30
    # Distrazione feed-browse su 2a tab.
    inbox_browser_feedbrowse_probability: float = 0.05
    inbox_browser_feedbrowse_min_seconds: int = 20
    inbox_browser_feedbrowse_max_seconds: int = 60
    # Quante chat raccolte prima del break di sessione (defer ARQ).
    inbox_session_size: int = 300
    inbox_break_min_minutes: int = 30
    inbox_break_max_minutes: int = 60
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_inbox_settings.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/config.py backend/tests/test_inbox_settings.py
git commit -m "feat(inbox): settings di pacing INBOX_* per engine browser/api"
```

---

### Task 2: Migrazione 019 + colonna `Campaign.inbox_engine`

**Files:**
- Modify: `backend/app/models/campaign.py:64` (dopo `scrape_cursor`)
- Create: `backend/alembic/versions/019_inbox_engine.py`
- Test: `backend/tests/test_inbox_engine_column.py`

**Interfaces:**
- Produces: `Campaign.inbox_engine: Mapped[str]` (String(10), default `'browser'`, not null).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_inbox_engine_column.py
"""Inbox: colonna inbox_engine sul modello Campaign."""
from app.models.campaign import Campaign


def test_inbox_engine_column_present():
    cols = Campaign.__table__.columns.keys()
    assert "inbox_engine" in cols


def test_inbox_engine_default_browser():
    col = Campaign.__table__.columns["inbox_engine"]
    assert col.default.arg == "browser"
    assert col.nullable is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_inbox_engine_column.py -v`
Expected: FAIL con `assert 'inbox_engine' in [...]`

- [ ] **Step 3: Add the model column**

In `backend/app/models/campaign.py`, subito dopo la riga 64 (`scrape_cursor: Mapped[str | None] = ...`), aggiungi:

```python
    # Engine di estrazione lista per scrape_mode=dm_threads: 'browser' (prudente,
    # default, account principali) | 'api' (veloce, account secondari). Vedi spec.
    inbox_engine: Mapped[str] = mapped_column(String(10), nullable=False, default='browser', server_default='browser')
```

- [ ] **Step 4: Create the Alembic migration**

```python
# backend/alembic/versions/019_inbox_engine.py
"""Inbox DM scraping: colonna inbox_engine su campaigns.

Revision ID: 019
Revises: 018
Create Date: 2026-06-23
"""
from alembic import op
import sqlalchemy as sa

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column("inbox_engine", sa.String(length=10), nullable=False, server_default="browser"),
    )


def downgrade() -> None:
    op.drop_column("campaigns", "inbox_engine")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && pytest tests/test_inbox_engine_column.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Apply the migration locally**

Run: `cd backend && alembic upgrade head`
Expected: `Running upgrade 018 -> 019, Inbox DM scraping: colonna inbox_engine su campaigns.`

> ⚠️ **Prod (Supabase):** la migrazione va applicata anche in prod prima del deploy del codice che legge/scrive `inbox_engine`, altrimenti Postgres 42703 (colonna inesistente). Vedi memoria progetto "TheVista — migrazione prima del deploy".

- [ ] **Step 7: Commit**

```bash
git add backend/app/models/campaign.py backend/alembic/versions/019_inbox_engine.py backend/tests/test_inbox_engine_column.py
git commit -m "feat(inbox): migrazione 019 + colonna Campaign.inbox_engine (default browser)"
```

---

### Task 3: Schemi Pydantic — `dm_threads`, `inbox_engine`, target opzionale

**Files:**
- Modify: `backend/app/schemas/campaign.py` (CampaignCreate riga 22 + validator 30-38; CampaignUpdate riga 53; CampaignResponse dopo riga 85)
- Modify: `backend/app/api/campaigns.py:172-191` (passare `inbox_engine` al costruttore `Campaign`)
- Test: `backend/tests/test_inbox_schema.py`

**Interfaces:**
- Consumes: `Campaign.inbox_engine` (Task 2).
- Produces: `CampaignCreate.scrape_mode` accetta `dm_threads`; `CampaignCreate.inbox_engine: str` (`browser|api`, default `browser`); `target_username` non obbligatorio quando `scrape_mode=='dm_threads'`; `CampaignResponse.inbox_engine`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_inbox_schema.py
"""Inbox: validazione schema CampaignCreate per dm_threads."""
import pytest
from pydantic import ValidationError
from app.schemas.campaign import CampaignCreate


def test_dm_threads_mode_accepted_without_target():
    c = CampaignCreate(name="x", scrape_mode="dm_threads", messaging_enabled=False)
    assert c.scrape_mode == "dm_threads"
    assert c.inbox_engine == "browser"  # default
    assert c.target_username is None


def test_inbox_engine_api_accepted():
    c = CampaignCreate(name="x", scrape_mode="dm_threads", inbox_engine="api", messaging_enabled=False)
    assert c.inbox_engine == "api"


def test_inbox_engine_invalid_rejected():
    with pytest.raises(ValidationError):
        CampaignCreate(name="x", scrape_mode="dm_threads", inbox_engine="selenium", messaging_enabled=False)


def test_scrape_mode_still_requires_target_for_followers():
    with pytest.raises(ValidationError):
        CampaignCreate(name="x", scrape_mode="followers", messaging_enabled=False)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_inbox_schema.py -v`
Expected: FAIL (pattern `scrape_mode` rifiuta `dm_threads`).

- [ ] **Step 3: Update CampaignCreate**

In `backend/app/schemas/campaign.py`:

Riga 22, sostituisci il pattern di `scrape_mode`:
```python
    scrape_mode: str = Field(default='followers', pattern='^(followers|following|dm_threads)$')
    # Engine estrazione lista per dm_threads (ignorato per followers/following).
    inbox_engine: str = Field(default='browser', pattern='^(browser|api)$')
```

Nel validator `_check_source` (righe 30-38), sostituisci la prima condizione perché `dm_threads` NON richieda `target_username`:
```python
    @model_validator(mode='after')
    def _check_source(self):
        if self.source_type == 'scrape' and self.scrape_mode != 'dm_threads' \
                and not (self.target_username and self.target_username.strip()):
            raise ValueError("target_username obbligatorio per source_type='scrape'")
        if self.messaging_enabled:
            t = (self.base_message_template or "").strip()
            if len(t) < 10:
                raise ValueError("base_message_template obbligatorio (min 10 caratteri) quando messaging_enabled=True")
        return self
```

- [ ] **Step 4: Update CampaignUpdate and CampaignResponse**

Riga 53, pattern di `scrape_mode` in `CampaignUpdate`:
```python
    scrape_mode: str | None = Field(default=None, pattern='^(followers|following|dm_threads)$')
    inbox_engine: str | None = Field(default=None, pattern='^(browser|api)$')
```

In `CampaignResponse`, dopo la riga 85 (`scrape_mode: str`), aggiungi:
```python
    inbox_engine: str = 'browser'
```

- [ ] **Step 5: Pass inbox_engine into the Campaign constructor**

In `backend/app/api/campaigns.py`, nel `create_campaign` (riga ~184), aggiungi dopo `scrape_mode=data.scrape_mode,`:
```python
        inbox_engine=data.inbox_engine,
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd backend && pytest tests/test_inbox_schema.py -v`
Expected: PASS (4 passed)

- [ ] **Step 7: Commit**

```bash
git add backend/app/schemas/campaign.py backend/app/api/campaigns.py backend/tests/test_inbox_schema.py
git commit -m "feat(inbox): schema dm_threads + inbox_engine, target opzionale"
```

---

### Task 4: Estrazione partecipante 1-a-1 (funzione pura condivisa)

**Files:**
- Create: `backend/app/services/inbox_source.py`
- Test: `backend/tests/test_inbox_participant.py`

**Interfaces:**
- Produces: `extract_thread_participant(thread_users, own_pk) -> tuple[int, str] | None` — ritorna `(ig_user_id, username)` per thread 1-a-1, `None` per gruppi (≠1 altro utente) o thread senza username. `thread_users` = lista di oggetti con attributi `.pk` e `.username` (DirectThread.users di instagrapi, o equivalenti dal browser).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_inbox_participant.py
"""Inbox: estrazione del partecipante 1-a-1 da un thread."""
from types import SimpleNamespace as NS
from app.services.inbox_source import extract_thread_participant

OWN = 999


def _u(pk, username):
    return NS(pk=pk, username=username)


def test_one_to_one_outbound():
    users = [_u(123, "mario")]  # noi non siamo in thread.users di instagrapi
    assert extract_thread_participant(users, OWN) == (123, "mario")


def test_one_to_one_with_self_present():
    users = [_u(OWN, "me"), _u(123, "mario")]
    assert extract_thread_participant(users, OWN) == (123, "mario")


def test_group_skipped():
    users = [_u(123, "mario"), _u(456, "lucia")]
    assert extract_thread_participant(users, OWN) is None


def test_empty_or_self_only_skipped():
    assert extract_thread_participant([], OWN) is None
    assert extract_thread_participant([_u(OWN, "me")], OWN) is None


def test_missing_username_skipped():
    assert extract_thread_participant([_u(123, None)], OWN) is None


def test_str_pk_coerced():
    assert extract_thread_participant([_u("123", "mario")], OWN) == (123, "mario")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_inbox_participant.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'app.services.inbox_source'`

- [ ] **Step 3: Write the implementation**

```python
# backend/app/services/inbox_source.py
"""Sorgente lista contatti dall'inbox DM (scrape_mode=dm_threads).

Espone la funzione pura di estrazione partecipante + l'interfaccia InboxListSource
con le due implementazioni (API/browser). Vedi spec 2026-06-23-inbox-dm-scraping.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


def extract_thread_participant(thread_users, own_pk: int) -> tuple[int, str] | None:
    """Ritorna (ig_user_id, username) per un thread 1-a-1, None per gruppi/invalidi.

    Perimetro Fase 1: solo 1-a-1 (esattamente un altro utente oltre a noi),
    entrambe le direzioni. `thread_users` puo' contenere o meno l'utente self
    (instagrapi spesso lo esclude); filtriamo own_pk in ogni caso.
    """
    others = []
    for u in thread_users or []:
        try:
            pk = int(u.pk)
        except (TypeError, ValueError, AttributeError):
            continue
        if pk == own_pk:
            continue
        others.append((pk, getattr(u, "username", None)))
    if len(others) != 1:
        return None
    pk, username = others[0]
    if not username:
        return None
    return (pk, username)


@dataclass
class InboxPage:
    """Una pagina di partecipanti estratti dall'inbox."""
    participants: list[tuple[int, str]] = field(default_factory=list)
    cursor: str | None = None      # stato di ripresa intra-engine (oldest_cursor o marker)
    exhausted: bool = False        # True quando l'inbox e' stato raggiunto fino all'inizio


class InboxListSource(Protocol):
    """Interfaccia comune alle due sorgenti inbox.

    next_page() restituisce la prossima pagina di partecipanti. La correttezza
    del riavvio (e dello switch engine) e' garantita a monte dal dedup sui
    Follower gia' salvati; cursor/marker sono solo ottimizzazione.
    """
    async def next_page(self) -> InboxPage: ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_inbox_participant.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/inbox_source.py backend/tests/test_inbox_participant.py
git commit -m "feat(inbox): estrazione partecipante 1-a-1 + interfaccia InboxListSource"
```

---

### Task 5: `ApiInboxSource` — paginazione `direct_v2/inbox` a cursore

**Files:**
- Modify: `backend/app/services/inbox_source.py` (aggiungi `fetch_inbox_page` + `ApiInboxSource`)
- Modify: `backend/app/adapters/instagram.py` (estendi il Protocol `IGClient`)
- Test: `backend/tests/test_inbox_api_source.py`

**Interfaces:**
- Consumes: `extract_thread_participant`, `InboxPage` (Task 4).
- Produces:
  - `fetch_inbox_page(client, cursor: str | None) -> tuple[list, str | None, bool]` — ritorna `(threads, next_cursor, has_older)`. `threads` = lista di oggetti con `.users`. Usa `client.private_request("direct_v2/inbox/", params=...)` parsando `inbox.threads` / `inbox.oldest_cursor` / `inbox.has_older`.
  - `ApiInboxSource(client, own_pk, cursor=None)` con `async def next_page() -> InboxPage`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_inbox_api_source.py
"""Inbox API source: paginazione a cursore + estrazione, con client mock."""
import pytest
from types import SimpleNamespace as NS
from app.services.inbox_source import ApiInboxSource

OWN = 999


def _u(pk, username):
    return NS(pk=pk, username=username)


def _thread(*users):
    return NS(users=list(users))


class FakeClient:
    """Mock di instagrapi.Client.private_request per direct_v2/inbox."""
    def __init__(self, pages):
        # pages = list di dict {"threads": [...], "oldest_cursor": str|None, "has_older": bool}
        self._pages = pages
        self.calls = []

    def private_request(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        page = self._pages.pop(0)
        return {
            "inbox": {
                "threads": page["threads"],
                "oldest_cursor": page["oldest_cursor"],
                "has_older": page["has_older"],
            }
        }


def _raw_thread(*users):
    return {"users": [{"pk": pk, "username": un} for pk, un in users]}


@pytest.mark.asyncio
async def test_api_source_first_page_extracts_participants():
    client = FakeClient([
        {"threads": [_raw_thread((123, "mario")), _raw_thread((456, "lucia"), (789, "gino"))],
         "oldest_cursor": "C1", "has_older": True},
    ])
    src = ApiInboxSource(client, OWN)
    page = await src.next_page()
    # gruppo (2 utenti) scartato, resta solo mario
    assert page.participants == [(123, "mario")]
    assert page.cursor == "C1"
    assert page.exhausted is False


@pytest.mark.asyncio
async def test_api_source_passes_cursor_and_detects_end():
    client = FakeClient([
        {"threads": [_raw_thread((123, "mario"))], "oldest_cursor": "C1", "has_older": True},
        {"threads": [_raw_thread((321, "anna"))], "oldest_cursor": None, "has_older": False},
    ])
    src = ApiInboxSource(client, OWN)
    p1 = await src.next_page()
    assert p1.exhausted is False
    p2 = await src.next_page()
    assert p2.participants == [(321, "anna")]
    assert p2.exhausted is True
    # la 2a chiamata deve aver passato il cursore della 1a
    assert client.calls[1][1].get("cursor") == "C1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_inbox_api_source.py -v`
Expected: FAIL con `ImportError: cannot import name 'ApiInboxSource'`

- [ ] **Step 3: Implement fetch_inbox_page + ApiInboxSource**

Aggiungi in `backend/app/services/inbox_source.py` (sopra `class InboxListSource`):

```python
import asyncio


def _as_users(raw_thread) -> list:
    """Normalizza thread.users sia da oggetti instagrapi sia da dict raw."""
    if isinstance(raw_thread, dict):
        users = raw_thread.get("users", [])
        from types import SimpleNamespace
        return [SimpleNamespace(pk=u.get("pk"), username=u.get("username")) for u in users]
    return getattr(raw_thread, "users", []) or []


def fetch_inbox_page(client, cursor: str | None) -> tuple[list, str | None, bool]:
    """Una pagina dell'inbox via private API. Ritorna (threads, next_cursor, has_older).

    Usa l'endpoint app direct_v2/inbox con thread_message_limit minimo: in Fase 1
    servono solo i partecipanti, non i messaggi -> payload leggero, meno crash parse.
    """
    params = {
        "visual_message_return_type": "unseen",
        "thread_message_limit": "1",
        "persistentBadging": "true",
        "limit": "20",
    }
    if cursor:
        params["cursor"] = cursor
        params["direction"] = "older"
    resp = client.private_request("direct_v2/inbox/", params=params)
    inbox = (resp or {}).get("inbox", {})
    threads = inbox.get("threads", []) or []
    next_cursor = inbox.get("oldest_cursor")
    has_older = bool(inbox.get("has_older"))
    return threads, next_cursor, has_older


class ApiInboxSource:
    """Sorgente inbox via instagrapi private API, paginata a oldest_cursor."""

    def __init__(self, client, own_pk: int, cursor: str | None = None):
        self._client = client
        self._own_pk = int(own_pk)
        self._cursor = cursor

    async def next_page(self) -> InboxPage:
        threads, next_cursor, has_older = await asyncio.to_thread(
            fetch_inbox_page, self._client, self._cursor
        )
        participants: list[tuple[int, str]] = []
        for t in threads:
            p = extract_thread_participant(_as_users(t), self._own_pk)
            if p is not None:
                participants.append(p)
        self._cursor = next_cursor
        exhausted = (not has_older) or (not next_cursor)
        return InboxPage(participants=participants, cursor=next_cursor, exhausted=exhausted)
```

- [ ] **Step 4: Extend the IGClient Protocol**

In `backend/app/adapters/instagram.py`, aggiungi al Protocol `IGClient`:
```python
    def private_request(self, path: str, params: dict | None = None) -> dict: ...
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && pytest tests/test_inbox_api_source.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/inbox_source.py backend/app/adapters/instagram.py backend/tests/test_inbox_api_source.py
git commit -m "feat(inbox): ApiInboxSource con paginazione direct_v2/inbox a cursore"
```

> **NB implementazione (verifica live):** i nomi esatti dei param/campi di `direct_v2/inbox/` possono variare con la versione instagrapi. Prima del rollout, verifica su un account reale che `private_request("direct_v2/inbox/", params)` ritorni `inbox.threads/oldest_cursor/has_older` e che `cursor`+`direction=older` paginino davvero verso il basso. Se instagrapi espone un helper paginato resumibile (`direct_threads_chunk`/simile) preferiscilo, mantenendo la stessa firma `fetch_inbox_page`.

---

### Task 6: `run_inbox_list` — Fase Lista inbox (loop, dedup, session-break) + engine API

**Files:**
- Create: `backend/app/services/scrape_inbox.py`
- Modify: `backend/app/services/scrape_list.py:79` (branch su `dm_threads`)
- Test: `backend/tests/test_scrape_inbox_loop.py`

**Interfaces:**
- Consumes: `ApiInboxSource`, `InboxPage` (Task 5); `isolate_challenged_account`, `is_challenge_exception` (`app.services.scraper`); `Follower`, `FollowerStatus`; `CampaignStatus`; `settings.inbox_*` (Task 1).
- Produces:
  - `async def run_inbox_list(campaign_id, db, campaign) -> int | None` — eseguito dentro la sessione DB già aperta da `list_followers`; ritorna i secondi di defer (pausa sessione) o `None` (completata/interrotta). Riusa stato `listing`/`listing_break`.
  - `async def build_inbox_source(db, campaign) -> tuple[source, own_pk, cleanup]` — costruisce `ApiInboxSource` o `BrowserInboxSource` (Task 7) in base a `campaign.inbox_engine`.
  - `def inbox_collect(participants, existing_ids) -> list[tuple[int,str]]` — funzione pura: filtra i partecipanti già presenti (dedup-frontier). Testabile a parte.

- [ ] **Step 1: Write the failing test (logica pura di dedup-frontier)**

```python
# backend/tests/test_scrape_inbox_loop.py
"""Inbox Fase Lista: dedup-frontier puro (correttezza riavvio/switch)."""
from app.services.scrape_inbox import inbox_collect


def test_collect_filters_already_saved():
    existing = {123, 456}
    page = [(123, "mario"), (789, "gino"), (456, "lucia"), (789, "gino")]
    # 123/456 gia' salvati -> scartati; 789 nuovo, dedup interno pagina -> una volta
    assert inbox_collect(page, existing) == [(789, "gino")]


def test_collect_all_new():
    assert inbox_collect([(1, "a"), (2, "b")], set()) == [(1, "a"), (2, "b")]


def test_collect_all_known_returns_empty():
    assert inbox_collect([(1, "a"), (2, "b")], {1, 2}) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_scrape_inbox_loop.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'app.services.scrape_inbox'`

- [ ] **Step 3: Implement scrape_inbox.py**

```python
# backend/app/services/scrape_inbox.py
"""Fase Lista alternativa per scrape_mode=dm_threads: raccoglie i contatti dai
DM gia' avviati dell'account. Engine selezionabile (api/browser). Riusa lo stato
listing/listing_break, il session-break via Retry(defer) e il challenge handler.
"""
import asyncio
import random
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import func, select

from app.config import settings
from app.models.campaign import Campaign, CampaignStatus
from app.models.campaign_account import CampaignAccount
from app.models.account import InstagramAccount, AccountStatus
from app.models.follower import Follower, FollowerStatus
from app.services.bot_state_service import is_halted
from app.services.scraper import is_challenge_exception, isolate_challenged_account
from app.services.inbox_source import ApiInboxSource
from app.utils.exceptions import BotHaltedError, ScrapeBudgetError, ScraperError
from app.utils.instagrapi_client import login as _login


def inbox_collect(participants, existing_ids) -> list[tuple[int, str]]:
    """Filtra i partecipanti gia' salvati (dedup-frontier) + dedup interno pagina.

    existing_ids = set di ig_user_id gia' presenti come Follower della campagna.
    Conserva l'ordine, prima occorrenza.
    """
    out: list[tuple[int, str]] = []
    seen: set[int] = set()
    for pk, username in participants:
        if pk in existing_ids or pk in seen:
            continue
        seen.add(pk)
        out.append((pk, username))
    return out


async def _single_inbox_account(db, campaign_id: str):
    """Ritorna l'unico account assegnato attivo per la campagna inbox, o solleva."""
    rows = (await db.execute(
        select(InstagramAccount)
        .join(CampaignAccount, CampaignAccount.account_id == InstagramAccount.id)
        .where(
            CampaignAccount.campaign_id == campaign_id,
            CampaignAccount.is_active == True,  # noqa: E712
            CampaignAccount.role.in_(("scraping", "both")),
            InstagramAccount.status.in_((AccountStatus.active, AccountStatus.warming_up)),
        )
    )).scalars().all()
    if len(rows) != 1:
        raise ScrapeBudgetError(
            f"Campagna inbox richiede esattamente 1 account attivo (trovati {len(rows)})"
        )
    return rows[0]


async def build_inbox_source(db, campaign):
    """Costruisce la sorgente inbox per l'engine scelto.

    Ritorna (source, own_pk, cleanup) dove cleanup e' una coroutine factory da
    awaitare nel finally (chiude browser / rilascia sessione).
    """
    account = await _single_inbox_account(db, campaign.id)
    engine = getattr(campaign, "inbox_engine", "browser") or "browser"

    if engine == "api":
        client = await _login(account, db)
        own_pk = int(client.user_id)
        # cursore valido solo per engine api (oldest_cursor)
        cursor = campaign.scrape_cursor or None
        source = ApiInboxSource(client, own_pk, cursor=cursor)

        async def _cleanup():
            return None

        return source, own_pk, _cleanup

    # engine == "browser"
    from app.services.inbox_browser_source import build_browser_inbox_source
    return await build_browser_inbox_source(db, campaign, account)


async def run_inbox_list(campaign_id: str, db, campaign) -> int | None:
    """Loop Fase Lista inbox. Eseguito dentro la sessione DB di list_followers.

    Ritorna i secondi di defer al raggiungimento del session-break (il worker
    solleva Retry(defer=...)); None se completata/interrotta.
    """
    from app.utils.events import emit as emit_event

    source = None
    cleanup = None
    account = None
    try:
        source, own_pk, cleanup = await build_inbox_source(db, campaign)
        emit_event(campaign_id, "scrape_start",
                   f"Fase Lista inbox avviata (engine {campaign.inbox_engine})")

        already = await db.scalar(
            select(func.count(Follower.id)).where(Follower.campaign_id == campaign_id)
        ) or 0
        existing_ids = set((await db.execute(
            select(Follower.ig_user_id).where(Follower.campaign_id == campaign_id)
        )).scalars().all())
        since_break = 0

        while True:
            if await is_halted(db):
                raise BotHaltedError("kill-switch")
            await db.refresh(campaign)
            if campaign.status not in (CampaignStatus.listing, CampaignStatus.listing_break):
                logger.info(f"[InboxLista] Stato '{campaign.status.value}' — interrotto a {already}")
                return None
            if campaign.list_target and already >= campaign.list_target:
                logger.info(f"[InboxLista] Target {campaign.list_target} raggiunto ({already})")
                break

            page = await source.next_page()
            fresh = inbox_collect(page.participants, existing_ids)
            for pk, username in fresh:
                db.add(Follower(
                    campaign_id=campaign_id,
                    ig_user_id=pk,
                    username=username,
                    full_name=None,
                    is_private=False,
                    is_verified=False,
                    profile_pic_url=None,
                    status=FollowerStatus.pending,
                ))
                existing_ids.add(pk)
            stored = len(fresh)
            already += stored
            since_break += stored
            # cursore intra-engine (api: oldest_cursor; browser: marker)
            campaign.scrape_cursor = page.cursor
            campaign.total_followers = already
            campaign.updated_at = datetime.utcnow()
            await db.commit()
            if stored:
                emit_event(campaign_id, "scrape_batch",
                           f"Inbox: {already}" + (f"/{campaign.list_target}" if campaign.list_target else ""))

            if page.exhausted:
                logger.info(f"[InboxLista] Inbox esaurito ({already})")
                campaign.scrape_cursor = None
                break

            # pacing API tra pagine (il browser gestisce il proprio pacing interno)
            if getattr(campaign, "inbox_engine", "browser") == "api":
                lo, hi = settings.inbox_api_page_delay_min_seconds, settings.inbox_api_page_delay_max_seconds
                await asyncio.sleep(random.uniform(lo, hi))

            if since_break >= settings.inbox_session_size:
                minutes = random.uniform(settings.inbox_break_min_minutes, settings.inbox_break_max_minutes)
                seconds = int(minutes * 60)
                campaign.scrape_break_prev_status = CampaignStatus.listing.value
                campaign.status = CampaignStatus.listing_break
                campaign.scrape_break_until = datetime.utcnow() + timedelta(seconds=seconds)
                campaign.updated_at = datetime.utcnow()
                await db.commit()
                emit_event(campaign_id, "scrape_break", f"Pausa inbox {int(minutes)} min dopo {already}")
                return seconds

        campaign.status = CampaignStatus.ready
        campaign.updated_at = datetime.utcnow()
        await db.commit()
        emit_event(campaign_id, "scrape_complete", f"Fase Lista inbox completata: {already} contatti in lista")
        return None

    except BotHaltedError:
        campaign.status = CampaignStatus.paused
        campaign.updated_at = datetime.utcnow()
        await db.commit()
        emit_event(campaign_id, "scrape_stopped", "Bot in pausa globale — inbox interrotta", level="warn")
        return None
    except (ScrapeBudgetError, ScraperError) as e:
        campaign.status = CampaignStatus.error
        campaign.updated_at = datetime.utcnow()
        await db.commit()
        emit_event(campaign_id, "scrape_stopped", f"Fase Lista inbox non avviata: {e}", level="error")
        return None
    except Exception as e:
        if is_challenge_exception(e) and account is not None:
            await isolate_challenged_account(db, campaign, account, e)
        else:
            logger.exception(f"[InboxLista] Errore campaign {campaign_id}: {e}")
            campaign.status = CampaignStatus.error
            campaign.updated_at = datetime.utcnow()
            await db.commit()
        return None
    finally:
        if cleanup is not None:
            try:
                await cleanup()
            except Exception as exc:
                logger.warning(f"[InboxLista] cleanup fallito: {exc}")
```

- [ ] **Step 4: Run the pure-logic test to verify it passes**

Run: `cd backend && pytest tests/test_scrape_inbox_loop.py -v`
Expected: PASS (3 passed)

> Nota: in questo step `build_inbox_source` importa `inbox_browser_source` solo nel ramo `browser`; il ramo API è autosufficiente. Il modulo browser arriva in Task 7. Il test qui copre solo `inbox_collect` (puro), quindi passa.

- [ ] **Step 5: Wire the dispatch in scrape_list.list_followers**

In `backend/app/services/scrape_list.py`, dentro `list_followers`, subito dopo il blocco di resume da `listing_break` (dopo la riga 77 `emit_event(campaign_id, "scrape_resume", ...)`) e PRIMA della riga 79 `scrape_mode = getattr(...)`, inserisci:

```python
        if getattr(campaign, "scrape_mode", "followers") == "dm_threads":
            from app.services.scrape_inbox import run_inbox_list
            return await run_inbox_list(campaign_id, db, campaign)
```

- [ ] **Step 6: Run the full backend suite for regressions**

Run: `cd backend && pytest tests/test_scrape_inbox_loop.py tests/test_two_phase_status.py tests/test_list_page_sizing.py -v`
Expected: PASS (tutti verdi — il branch non tocca il path follower/following).

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/scrape_inbox.py backend/app/services/scrape_list.py backend/tests/test_scrape_inbox_loop.py
git commit -m "feat(inbox): run_inbox_list (loop+dedup+session-break) + dispatch da list_followers"
```

---

### Task 7: `BrowserInboxSource` — scroll Patchright dell'inbox

**Files:**
- Create: `backend/app/services/inbox_browser_source.py`
- Modify: `backend/app/browser/instagram_page.py` (nuovo metodo `scroll_inbox_threads`)
- Test: `backend/tests/test_inbox_browser_source.py`

**Interfaces:**
- Consumes: `extract_thread_participant`, `InboxPage` (Task 4); `InstagramPage` POM (`app.browser.instagram_page`); `context_manager` per il browser context.
- Produces:
  - `class BrowserInboxSource` con `async def next_page() -> InboxPage` — ogni chiamata scrolla un blocco di righe nuove e ritorna i partecipanti raccolti; `exhausted=True` quando lo scroll non produce più righe nuove.
  - `async def build_browser_inbox_source(db, campaign, account) -> tuple[source, own_pk, cleanup]`.
  - Funzione pura testabile: `parse_thread_rows(rows_data, own_pk) -> list[tuple[int,str]]` dove `rows_data` = lista di dict `{"pk":..., "username":...}` estratti dal DOM.

> **Verifica live obbligatoria:** i selettori DOM della lista DM di Instagram (`/direct/inbox/`) non sono stabili e vanno confermati su sessione reale prima del rollout. Questo task implementa e testa la **logica pura** (parsing righe, dedup-frontier, rilevamento fine-scroll) e isola i selettori in un unico metodo POM (`scroll_inbox_threads`) da tarare live. NON marcare il task completo finché lo scroll reale non è verificato su un account di test.

- [ ] **Step 1: Write the failing test (logica pura)**

```python
# backend/tests/test_inbox_browser_source.py
"""Inbox browser source: parsing righe DOM + dedup-frontier (logica pura)."""
import pytest
from app.services.inbox_browser_source import parse_thread_rows, BrowserInboxSource

OWN = 999


def test_parse_rows_one_to_one():
    rows = [{"pk": 123, "username": "mario"}, {"pk": 456, "username": "lucia"}]
    assert parse_thread_rows(rows, OWN) == [(123, "mario"), (456, "lucia")]


def test_parse_rows_skips_self_and_groupish():
    rows = [{"pk": OWN, "username": "me"}, {"pk": None, "username": None}, {"pk": "789", "username": "gino"}]
    assert parse_thread_rows(rows, OWN) == [(789, "gino")]


class FakePage:
    """Simula scroll_inbox_threads: ritorna blocchi di righe finche' esauriti."""
    def __init__(self, blocks):
        self._blocks = list(blocks)
        self.scrolls = 0

    async def scroll_inbox_threads(self):
        self.scrolls += 1
        if not self._blocks:
            return []
        return self._blocks.pop(0)


@pytest.mark.asyncio
async def test_browser_source_paginates_until_empty():
    page = FakePage([
        [{"pk": 1, "username": "a"}, {"pk": 2, "username": "b"}],
        [{"pk": 3, "username": "c"}],
        [],  # nessuna riga nuova -> fine
    ])
    src = BrowserInboxSource(page, OWN)
    p1 = await src.next_page()
    assert p1.participants == [(1, "a"), (2, "b")]
    assert p1.exhausted is False
    p2 = await src.next_page()
    assert p2.participants == [(3, "c")]
    p3 = await src.next_page()
    assert p3.participants == []
    assert p3.exhausted is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_inbox_browser_source.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'app.services.inbox_browser_source'`

- [ ] **Step 3: Implement inbox_browser_source.py**

```python
# backend/app/services/inbox_browser_source.py
"""Sorgente inbox via browser Patchright: scroll della lista DM /direct/inbox/.

La logica pura (parsing righe, dedup, fine-scroll) e' qui e testabile; i selettori
DOM vivono in InstagramPage.scroll_inbox_threads (da verificare live).
"""
from loguru import logger

from app.services.inbox_source import InboxPage


def parse_thread_rows(rows_data, own_pk: int) -> list[tuple[int, str]]:
    """Da righe DOM (dict pk/username) a lista (ig_user_id, username) 1-a-1 valide."""
    out: list[tuple[int, str]] = []
    for row in rows_data or []:
        pk = row.get("pk")
        username = row.get("username")
        try:
            pk = int(pk)
        except (TypeError, ValueError):
            continue
        if pk == int(own_pk) or not username:
            continue
        out.append((pk, username))
    return out


class BrowserInboxSource:
    """Sorgente inbox via scroll del DOM. Ogni next_page() = un blocco di scroll.

    exhausted quando uno scroll non produce piu' righe (lista virtualizzata in fondo).
    La de-duplicazione globale resta a carico di run_inbox_list (existing_ids), qui
    si fa solo de-dup locale per non riemettere le stesse righe ancora a schermo.
    """

    def __init__(self, page, own_pk: int):
        self._page = page
        self._own_pk = int(own_pk)
        self._seen: set[int] = set()

    async def next_page(self) -> InboxPage:
        rows = await self._page.scroll_inbox_threads()
        parsed = parse_thread_rows(rows, self._own_pk)
        fresh: list[tuple[int, str]] = []
        for pk, username in parsed:
            if pk in self._seen:
                continue
            self._seen.add(pk)
            fresh.append((pk, username))
        exhausted = len(rows or []) == 0
        # marker di profondita' best-effort: numero righe viste (non un cursore IG)
        marker = str(len(self._seen))
        return InboxPage(participants=fresh, cursor=marker, exhausted=exhausted)


async def build_browser_inbox_source(db, campaign, account):
    """Apre il browser sull'inbox dell'account e ritorna (source, own_pk, cleanup)."""
    from app.browser.context_manager import get_context
    from app.browser.instagram_page import InstagramPage
    from app.utils.instagrapi_client import login as _login

    # own_pk: ricavato via instagrapi (login leggero) per coerenza con engine api.
    client = await _login(account, db)
    own_pk = int(client.user_id)

    ctx = await get_context(account.id)
    pom = InstagramPage(ctx)
    await pom.ensure_logged_in(account.id)
    await pom.open_inbox()  # naviga a /direct/inbox/ (vedi Step 4)
    source = BrowserInboxSource(pom, own_pk)

    async def _cleanup():
        try:
            await pom.close_inbox_tabs()
        finally:
            from app.browser.context_manager import release_context
            await release_context(account.id)

    return source, own_pk, _cleanup
```

> Adatta `get_context` / `release_context` / costruttore `InstagramPage` ai nomi reali in `app/browser/context_manager.py` e `instagram_page.py` (leggi quei file: il POM è inizializzato con il `context` e `ensure_logged_in(account_id)` esiste già — vedi `instagram_page.py:48,59`). Se il context manager espone un mutex/acquire diverso, usalo: **un solo browser per account** è già garantito dal mutex per-account esistente.

- [ ] **Step 4: Add the POM scroll method (selettori da verificare live)**

In `backend/app/browser/instagram_page.py` aggiungi i metodi (i selettori sono un primo tentativo, da confermare su IG reale):

```python
    async def open_inbox(self) -> None:
        page = await self._get_page()
        await page.goto(f"{self.BASE_URL}/direct/inbox/", wait_until="domcontentloaded")
        await self._dismiss_ig_modals(page, "inbox")

    async def scroll_inbox_threads(self) -> list[dict]:
        """Scrolla la lista DM di un blocco ed estrae le righe attualmente nel DOM.

        Ritorna [{"pk": int|str, "username": str}] per i thread 1-a-1 visibili.
        Lista virtualizzata: estrae a OGNI chiamata (i nodi vengono riciclati).
        SELETTORI DA VERIFICARE LIVE.
        """
        page = await self._get_page()
        rows = await page.evaluate(
            """
            () => {
              const out = [];
              const links = document.querySelectorAll('div[role="list"] a[href^="/direct/t/"], a[href^="/direct/t/"]');
              for (const a of links) {
                // username: spesso nel testo del primo span con dir="auto"
                const span = a.querySelector('span[dir="auto"]');
                const username = span ? span.textContent.trim() : null;
                // pk non e' nel DOM in chiaro: si usa il thread come chiave e si
                // risolve il pk lato server. Qui ritorniamo username; il pk viene
                // risolto via instagrapi user_id_from_username nel chiamante se serve.
                out.push({ username, href: a.getAttribute('href') });
              }
              return out;
            }
            """
        )
        # scroll del pannello lista verso il basso per caricare thread piu' vecchi
        await page.evaluate(
            """() => {
              const list = document.querySelector('div[role="list"]') || document.scrollingElement;
              if (list) list.scrollBy(0, list.clientHeight * 0.8);
            }"""
        )
        # NB: la risoluzione username->pk e l'estrazione robusta vanno tarate live.
        return [{"pk": None, "username": r.get("username")} for r in rows if r.get("username")]
```

> ⚠️ **Limite noto da risolvere live:** il DOM della lista DM **non espone il pk numerico** dell'utente, solo l'username (e il `thread_id` nell'href). Due strade, da decidere in fase di verifica live: (a) raccogliere gli **username** nel browser e risolvere il `pk` lato server via instagrapi `user_id_from_username` (1 chiamata API leggera per utente — accettabile, ma reintroduce un po' di API); (b) salvare il `Follower` con `ig_user_id` risolto in Fase Bio. Per Fase 1, la scelta (a) mantiene il dedup su `ig_user_id` coerente con l'engine API. Aggiorna `parse_thread_rows`/`BrowserInboxSource` di conseguenza una volta verificato il DOM. Documenta la scelta finale nello spec.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && pytest tests/test_inbox_browser_source.py -v`
Expected: PASS (3 passed) — i test coprono la logica pura, non i selettori.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/inbox_browser_source.py backend/app/browser/instagram_page.py backend/tests/test_inbox_browser_source.py
git commit -m "feat(inbox): BrowserInboxSource (scroll DOM) — logica pura testata, selettori da verificare live"
```

---

### Task 8: Guard 1-account (start + assegnazione account → HTTP 400)

**Files:**
- Modify: `backend/app/api/campaigns.py` (`start_list`, riga ~489) — guard esatto-1-account per `dm_threads`.
- Modify: `backend/app/api/campaign_accounts.py` — rifiuta il 2° account su campagna `dm_threads`.
- Test: `backend/tests/test_inbox_one_account_guard.py`

**Interfaces:**
- Consumes: `Campaign.scrape_mode`, `CampaignAccount`.
- Produces: `def inbox_account_count_ok(scrape_mode, active_count) -> bool` (pura) in `app/api/campaigns.py`; usata sia in `start_list` sia (via import) nell'assegnazione.

- [ ] **Step 1: Write the failing test (logica pura del guard)**

```python
# backend/tests/test_inbox_one_account_guard.py
"""Inbox: guard 1-account per scrape_mode=dm_threads."""
from app.api.campaigns import inbox_account_count_ok


def test_dm_threads_requires_exactly_one():
    assert inbox_account_count_ok("dm_threads", 1) is True
    assert inbox_account_count_ok("dm_threads", 0) is False
    assert inbox_account_count_ok("dm_threads", 2) is False


def test_other_modes_not_constrained():
    assert inbox_account_count_ok("followers", 0) is True
    assert inbox_account_count_ok("followers", 3) is True
    assert inbox_account_count_ok("following", 2) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_inbox_one_account_guard.py -v`
Expected: FAIL con `ImportError: cannot import name 'inbox_account_count_ok'`

- [ ] **Step 3: Add the pure guard + wire into start_list**

In `backend/app/api/campaigns.py`, vicino alle altre helper in cima al modulo (dopo `list_start_blocked`, riga ~70), aggiungi:

```python
def inbox_account_count_ok(scrape_mode: str, active_count: int) -> bool:
    """dm_threads richiede ESATTAMENTE 1 account attivo; altre modalita' libere."""
    if scrape_mode == "dm_threads":
        return active_count == 1
    return True
```

In `start_list`, dopo il check `has_active_role_account` (riga ~490), aggiungi il guard esatto-1 per inbox:

```python
    if campaign.scrape_mode == "dm_threads":
        active_count = await db.scalar(
            select(func.count(CampaignAccount.account_id))
            .join(InstagramAccount, InstagramAccount.id == CampaignAccount.account_id)
            .where(
                CampaignAccount.campaign_id == campaign_id,
                CampaignAccount.is_active == True,  # noqa: E712
                CampaignAccount.role.in_(("scraping", "both")),
                InstagramAccount.status.in_((AccountStatus.active, AccountStatus.warming_up)),
            )
        ) or 0
        if not inbox_account_count_ok("dm_threads", active_count):
            raise HTTPException(
                status_code=400,
                detail=f"Campagna inbox (DM): serve esattamente 1 account attivo (trovati {active_count}).",
            )
```

> Verifica gli import in testa a `campaigns.py`: servono `InstagramAccount`, `AccountStatus` (già importati per altri guard; se manca `AccountStatus`, aggiungi `from app.models.account import InstagramAccount, AccountStatus`).

- [ ] **Step 4: Wire the guard into account assignment**

Leggi `backend/app/api/campaign_accounts.py` e individua l'endpoint che crea una riga `CampaignAccount` (assegnazione account→campagna). Prima dell'insert, aggiungi:

```python
    # Hard-cap 1 account per le campagne inbox (dm_threads).
    if campaign.scrape_mode == "dm_threads":
        existing = await db.scalar(
            select(func.count(CampaignAccount.account_id))
            .where(CampaignAccount.campaign_id == campaign_id)
        ) or 0
        if existing >= 1:
            raise HTTPException(
                status_code=400,
                detail="Le campagne inbox (DM) ammettono un solo account.",
            )
```

(Assicura gli import `select`, `func`, `HTTPException`, `CampaignAccount`, e il caricamento di `campaign` nell'endpoint.)

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && pytest tests/test_inbox_one_account_guard.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/campaigns.py backend/app/api/campaign_accounts.py backend/tests/test_inbox_one_account_guard.py
git commit -m "feat(inbox): guard hard 1-account (start + assegnazione 400)"
```

---

### Task 9: Switch engine a metà campagna (PATCH, resume-by-frontier)

**Files:**
- Modify: `backend/app/schemas/campaign.py` (`CampaignUpdate.inbox_engine` già aggiunto in Task 3)
- Modify: `backend/app/api/campaigns.py` (`update_campaign`, righe ~269-300) — applica `inbox_engine` solo a campagna in pausa/ready, azzera `scrape_cursor` al cambio.
- Test: `backend/tests/test_inbox_engine_switch.py`

**Interfaces:**
- Consumes: `Campaign.inbox_engine`, `Campaign.scrape_cursor`, `CampaignUpdate.inbox_engine`.
- Produces: `def engine_switch_resets_cursor(old_engine, new_engine) -> bool` (pura) in `campaigns.py`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_inbox_engine_switch.py
"""Inbox: cambio engine azzera il cursore intra-engine (resume-by-frontier)."""
from app.api.campaigns import engine_switch_resets_cursor


def test_switch_resets_cursor():
    assert engine_switch_resets_cursor("browser", "api") is True
    assert engine_switch_resets_cursor("api", "browser") is True


def test_same_engine_keeps_cursor():
    assert engine_switch_resets_cursor("browser", "browser") is False
    assert engine_switch_resets_cursor("api", "api") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_inbox_engine_switch.py -v`
Expected: FAIL con `ImportError: cannot import name 'engine_switch_resets_cursor'`

- [ ] **Step 3: Add the pure helper + wire into update_campaign**

In `backend/app/api/campaigns.py`, vicino a `inbox_account_count_ok`, aggiungi:

```python
def engine_switch_resets_cursor(old_engine: str, new_engine: str) -> bool:
    """True se il cambio engine invalida il cursore (token non interscambiabili)."""
    return old_engine != new_engine
```

In `update_campaign`, nella sezione che applica i campi opzionali (dopo il blocco `scrape_mode`, riga ~270), aggiungi:

```python
    if data.inbox_engine is not None:
        if campaign.status not in (CampaignStatus.draft, CampaignStatus.ready, CampaignStatus.paused, CampaignStatus.error):
            raise HTTPException(
                status_code=400,
                detail="L'engine inbox si cambia solo a campagna ferma (draft/ready/paused/error).",
            )
        if engine_switch_resets_cursor(campaign.inbox_engine, data.inbox_engine):
            campaign.scrape_cursor = None  # cursore vecchio non valido per il nuovo engine
        campaign.inbox_engine = data.inbox_engine
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_inbox_engine_switch.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Run the full inbox suite**

Run: `cd backend && pytest tests/test_inbox_settings.py tests/test_inbox_engine_column.py tests/test_inbox_schema.py tests/test_inbox_participant.py tests/test_inbox_api_source.py tests/test_scrape_inbox_loop.py tests/test_inbox_browser_source.py tests/test_inbox_one_account_guard.py tests/test_inbox_engine_switch.py -v`
Expected: PASS (tutti verdi).

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/campaigns.py backend/tests/test_inbox_engine_switch.py
git commit -m "feat(inbox): switch engine a campagna ferma + reset cursore (resume-by-frontier)"
```

---

### Task 10: Frontend — modalità inbox nel form + radio engine + switch

**Files:**
- Modify: `frontend/lib/types.ts` — aggiungi `inbox_engine` ai tipi Campaign/CampaignCreate.
- Modify: `frontend/lib/api.ts` — assicura che create/update inoltrino `scrape_mode` e `inbox_engine`.
- Modify: `frontend/app/campaigns/new/page.tsx` — opzione `scrape_mode = "DM già avviati (inbox)"`, radio engine, nascondi `target_username`, limita a 1 account.
- Modify: `frontend/app/campaigns/[id]/page.tsx` — controllo per cambiare engine a campagna ferma (warning su `api→browser`).

**Interfaces:**
- Consumes: API backend dei Task 3/8/9.

> Frontend senza test automatici nel repo: la verifica è manuale (Step 4). Segui i pattern esistenti del form `new/page.tsx` (leggilo prima: usa stato locale + `createCampaign` da `lib/api.ts`).

- [ ] **Step 1: Add the type fields**

In `frontend/lib/types.ts`, nel tipo della campagna e del payload di creazione, aggiungi:
```ts
  scrape_mode: 'followers' | 'following' | 'dm_threads'
  inbox_engine?: 'browser' | 'api'
```

- [ ] **Step 2: Add the mode option + engine radio in the new-campaign form**

In `frontend/app/campaigns/new/page.tsx`:
- aggiungi `dm_threads` al selettore `scrape_mode` con label "DM già avviati (inbox)";
- quando `scrape_mode === 'dm_threads'`: nascondi l'input `target_username` (non inviarlo / inviare `null`), mostra un radio:
  - "🛡️ Browser (prudente, lento)" → `inbox_engine = 'browser'` (default selezionato)
  - "⚡ API (veloce, più rischio)" → `inbox_engine = 'api'`;
- includi `inbox_engine` nel payload `createCampaign`.

Esempio di blocco condizionale (adatta alla struttura JSX/stato esistente del file):
```tsx
{scrapeMode === 'dm_threads' && (
  <div className="space-y-2">
    <label className="block text-sm font-medium">Engine estrazione lista</label>
    <label className="flex items-center gap-2">
      <input type="radio" name="inboxEngine" value="browser"
             checked={inboxEngine === 'browser'} onChange={() => setInboxEngine('browser')} />
      🛡️ Browser (prudente, lento) — consigliato per account principali
    </label>
    <label className="flex items-center gap-2">
      <input type="radio" name="inboxEngine" value="api"
             checked={inboxEngine === 'api'} onChange={() => setInboxEngine('api')} />
      ⚡ API (veloce, più rischio) — solo account secondari
    </label>
  </div>
)}
```

- [ ] **Step 3: Add the mid-campaign engine switch on the detail page**

In `frontend/app/campaigns/[id]/page.tsx`, per campagne `scrape_mode === 'dm_threads'` in stato fermo (`draft`/`ready`/`paused`/`error`), mostra un controllo che chiama `updateCampaign(id, { inbox_engine })`. Se l'utente passa da `api` a `browser`, mostra un avviso: "Sconsigliato su inbox grandi: il browser deve riattraversare quanto già fatto."

- [ ] **Step 4: Manual verification**

```bash
cd frontend && npm run dev
```
Verifica nel browser (http://localhost:3000):
1. Nuova campagna → scegli "DM già avviati (inbox)" → l'input target sparisce, appare il radio engine (browser preselezionato).
2. Assegna 1 account → start Fase Lista parte; prova ad assegnarne un 2° → errore 400 "un solo account".
3. Metti in pausa → cambia engine da browser ad api → ricontrolla che riparta (resume-by-frontier, nessun duplicato in lista).

- [ ] **Step 5: Commit**

```bash
git add frontend/lib/types.ts frontend/lib/api.ts frontend/app/campaigns/new/page.tsx frontend/app/campaigns/[id]/page.tsx
git commit -m "feat(inbox): UI modalità DM inbox — radio engine + switch a campagna ferma"
```

---

## Self-Review

**Spec coverage:**
- §2 perimetro 1-a-1 entrambe direzioni → Task 4 (`extract_thread_participant`), Task 6 (insert).
- §2 profondità a sessioni → Task 6 (session-break via defer, `inbox_session_size`).
- §2/§4.1 engine selezionabile → Task 2/3 (`inbox_engine`), Task 6 (`build_inbox_source`).
- §4.1/§4.5 resume-by-frontier + switch → Task 6 (`inbox_collect`/`existing_ids`), Task 9 (reset cursore).
- §4.2 modulo scrape_inbox + riuso macchina listing → Task 6 (dispatch da `list_followers`, stato `listing`/`listing_break`).
- §4.3 estrazione partecipante → Task 4.
- §4.4 Fase Bio invariata → nessuna modifica (i `Follower(pending)` prodotti sono già consumati da `scrape_bios.py`).
- §5 dati (colonna + cursore riusato) → Task 2, Task 6/9.
- §6 anti-detection (pacing API/browser) → Task 1, Task 6/7.
- §6.1 pacing browser (scroll/micro-pause/feed-tab/marker) → Task 1 (settings), Task 7 (scroll), **micro-pause/feed-tab da cablare nel metodo `scroll_inbox_threads` in fase di verifica live** (Task 7 Step 4: i settings esistono, l'integrazione delle micro-pause va completata quando i selettori sono confermati).
- §7 guard 1-account 3 livelli → Task 8 (start + assegnazione), Task 10 (UI).
- §8 bio su API → invariata, by design.
- §9 frontend → Task 10.
- §10 test → Task 4/5/6/7/8/9 (unit); switch e guard coperti.

**Gap dichiarati (onestà):**
- Le **micro-pause in-place e il feed-browse su 2ª tab** (§6.1) sono presenti come *settings* (Task 1) ma la loro integrazione effettiva vive dentro `scroll_inbox_threads`, che è marcato "verifica live" perché dipende dai selettori reali. Vanno completati nello stesso passaggio in cui si tarano i selettori. Questo è esplicito in Task 7 e qui — non è un placeholder nascosto.
- La **risoluzione username→pk** nell'engine browser (DOM non espone il pk) è un punto aperto reale documentato in Task 7 Step 4; la scelta (a) (risoluzione server-side) è raccomandata e va confermata live.

**Placeholder scan:** nessun "TBD/TODO" mascherato; i due punti "verifica live" sopra sono limiti tecnici reali ed espliciti, non scorciatoie di pianificazione.

**Type consistency:** `InboxPage(participants, cursor, exhausted)` usato coerente in Task 4/5/6/7. `extract_thread_participant(users, own_pk)` firma stabile. `inbox_account_count_ok` / `engine_switch_resets_cursor` firme coerenti tra definizione e test. `Follower` creato solo con campi reali verificati in `scrape_list.py:153-162`.
