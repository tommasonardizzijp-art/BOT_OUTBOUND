# Advanced Scraping & Contact Harvesting — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Estrarre e persistere i contatti completi (telefono, email, WhatsApp, tutti i link bio) ad ogni scraping, salvarli universalmente su `followers` + `global_contacts`, rendere la messaggistica opzionale per campagna, aggiungere un cap anti-ban lookup/account e un export lead filtrabile per campagna/account.

**Architecture:** Un modulo puro `contact_extract.py` mappa l'oggetto `user_info` (già scaricato in 1 call IG) in una `ContactData`, usato sia dallo scraper sia dal resolver import (parità di comportamento). Un servizio `global_contact_service.upsert_lead` registra ogni profilo scrapato come "lead visto" in `global_contacts` con merge cross-campagna. Un cap lookup/giorno/account (con rotazione) frena lo scraping senza toccare i meccanismi anti-detection esistenti. Toggle `messaging_enabled` su `campaigns` rende il template opzionale e porta le campagne lead-only a `completed`.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.x async, Alembic (Supabase Postgres), ARQ, instagrapi, pytest. Frontend Next.js 14 + TypeScript.

**Spec di riferimento:** `docs/superpowers/specs/2026-06-06-advanced-scraping-contacts-design.md`

**Branch:** `feature/advanced-scraping` (già creato).

---

## Convenzioni di esecuzione

- Attivare il venv prima di ogni comando Python: `cd backend && ./venv/Scripts/activate` (Windows).
- I test sono **unit puri** dove possibile (nessun DB), come il resto della suite (`tests/test_ig_username.py`, `tests/test_import_resolver.py`).
- Le verifiche di integrazione usano `python -c "import ..."` / `python -m py_compile`, coerente con lo stile del repo.
- Commit frequenti, uno per task completato.
- **NON** modificare timing/distribuzioni/session-break esistenti. Il cap è un freno aggiuntivo.

---

## Mappa file

### Creati
- `backend/app/utils/contact_extract.py` — estrazione contatti pura
- `backend/app/services/global_contact_service.py` — upsert + merge lead
- `backend/alembic/versions/014_advanced_scraping_contacts.py` — migrazione
- `backend/tests/test_contact_extract.py`
- `backend/tests/test_global_contact_merge.py`
- `backend/tests/test_scrape_cap.py`
- `backend/tests/test_campaign_messaging_toggle.py`

### Modificati (backend)
- `backend/app/models/follower.py` — colonne contatto
- `backend/app/models/global_contact.py` — colonne contatto + scrape_sources + first_seen_at + contact_source
- `backend/app/models/campaign.py` — `messaging_enabled`, `scrape_daily_limit`, template nullable
- `backend/app/models/account.py` — `scrape_lookups_today`
- `backend/app/config.py` — `scrape_daily_limit: int = 180`
- `backend/app/utils/exceptions.py` — `ScrapeBudgetError`
- `backend/app/services/account_manager.py` — helper cap scraping
- `backend/app/services/scraper.py` — estrazione + upsert + cap
- `backend/app/services/import_resolver.py` — estrazione + upsert + cap
- `backend/app/services/campaign_orchestrator.py` — merge contatti a send-time
- `backend/app/workers/task_queue.py` — reset `scrape_lookups_today` nel `daily_reset`
- `backend/app/api/campaigns.py` — toggle/guard/stato finale
- `backend/app/api/leads.py` — colonne export + filtri multi-select
- `backend/app/schemas/campaign.py` — nuovi campi
- `backend/app/schemas/follower.py` — campi contatto
- `backend/app/schemas/lead.py` — campi contatto
- `backend/.env.example` — `SCRAPE_DAILY_LIMIT=180`

### Modificati (frontend)
- `frontend/src/lib/types.ts`, `frontend/src/lib/api.ts`
- `frontend/src/components/campaigns/CampaignForm.tsx` (o percorso equivalente)
- `frontend/src/app/leads/page.tsx` (pagina leads)

### Docs
- `CLAUDE.md`, `INDEX.md`, `docs/project/PROGRESS.md`
- memoria `C:\Users\39342\.claude\projects\d--BOT-OUTBOUND\memory\project_state.md` + `MEMORY.md`

---

## Task 1: Migrazione 014 + colonne nei modelli

**Files:**
- Create: `backend/alembic/versions/014_advanced_scraping_contacts.py`
- Modify: `backend/app/models/follower.py`
- Modify: `backend/app/models/global_contact.py`
- Modify: `backend/app/models/campaign.py`
- Modify: `backend/app/models/account.py`

- [ ] **Step 1: Scrivere la migrazione 014**

Create `backend/alembic/versions/014_advanced_scraping_contacts.py`:

```python
"""Advanced scraping: contact columns + messaging toggle + scrape cap.

Revision ID: 014
Revises: 013
Create Date: 2026-06-06
"""
from alembic import op
import sqlalchemy as sa

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # followers: contact columns
    op.add_column("followers", sa.Column("phone", sa.String(64), nullable=True))
    op.add_column("followers", sa.Column("email", sa.String(255), nullable=True))
    op.add_column("followers", sa.Column("whatsapp", sa.String(255), nullable=True))
    op.add_column("followers", sa.Column("bio_links", sa.Text(), nullable=True))
    op.add_column("followers", sa.Column("contact_source", sa.Text(), nullable=True))
    op.add_column("followers", sa.Column("contact_extra", sa.Text(), nullable=True))

    # global_contacts: contact columns + provenance
    op.add_column("global_contacts", sa.Column("phone", sa.String(64), nullable=True))
    op.add_column("global_contacts", sa.Column("email", sa.String(255), nullable=True))
    op.add_column("global_contacts", sa.Column("whatsapp", sa.String(255), nullable=True))
    op.add_column("global_contacts", sa.Column("bio_links", sa.Text(), nullable=True))
    op.add_column("global_contacts", sa.Column("external_url", sa.String(512), nullable=True))
    op.add_column("global_contacts", sa.Column("contact_source", sa.Text(), nullable=True))
    op.add_column("global_contacts", sa.Column("contact_extra", sa.Text(), nullable=True))
    op.add_column("global_contacts", sa.Column("scrape_sources", sa.Text(), nullable=False, server_default="[]"))
    op.add_column("global_contacts", sa.Column("first_seen_at", sa.DateTime(), nullable=True))

    # campaigns: messaging toggle + scrape cap override + template nullable
    op.add_column("campaigns", sa.Column("messaging_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("campaigns", sa.Column("scrape_daily_limit", sa.Integer(), nullable=True))
    op.alter_column("campaigns", "base_message_template", existing_type=sa.Text(), nullable=True)

    # instagram_accounts: daily scrape lookup counter
    op.add_column("instagram_accounts", sa.Column("scrape_lookups_today", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("instagram_accounts", "scrape_lookups_today")
    op.alter_column("campaigns", "base_message_template", existing_type=sa.Text(), nullable=False)
    op.drop_column("campaigns", "scrape_daily_limit")
    op.drop_column("campaigns", "messaging_enabled")
    for col in ("first_seen_at", "scrape_sources", "contact_extra", "contact_source",
                "external_url", "bio_links", "whatsapp", "email", "phone"):
        op.drop_column("global_contacts", col)
    for col in ("contact_extra", "contact_source", "bio_links", "whatsapp", "email", "phone"):
        op.drop_column("followers", col)
```

- [ ] **Step 2: Aggiungere le colonne al modello `Follower`**

In `backend/app/models/follower.py`, dopo la riga `external_url` (riga 38), aggiungere:

```python
    # Contact info (advanced scraping). Populated from user_info at scrape time.
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    whatsapp: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bio_links: Mapped[str | None] = mapped_column(Text, nullable=True)        # JSON: [{"url","title"}]
    contact_source: Mapped[str | None] = mapped_column(Text, nullable=True)   # JSON: {"phone":"ig_business",...}
    contact_extra: Mapped[str | None] = mapped_column(Text, nullable=True)    # JSON, reserved Fase 2
```

- [ ] **Step 3: Aggiungere le colonne al modello `GlobalContact`**

In `backend/app/models/global_contact.py`, dopo `biography` (riga 15), aggiungere:

```python
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    whatsapp: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bio_links: Mapped[str | None] = mapped_column(Text, nullable=True)        # JSON
    external_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    contact_source: Mapped[str | None] = mapped_column(Text, nullable=True)   # JSON per-field source
    contact_extra: Mapped[str | None] = mapped_column(Text, nullable=True)    # JSON, reserved Fase 2
    # [{campaign_id, campaign_name, scraping_account_id, scraping_account_username, scraped_at}]
    scrape_sources: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
```

Aggiungere `Integer` agli import se assente (riga 3 importa già `String, BigInteger, DateTime, Text`; non serve Integer qui).

- [ ] **Step 4: Aggiungere le colonne al modello `Campaign`**

In `backend/app/models/campaign.py`:

Cambiare `base_message_template` (riga 30) da:
```python
    base_message_template: Mapped[str] = mapped_column(Text, nullable=False)
```
a:
```python
    base_message_template: Mapped[str | None] = mapped_column(Text, nullable=True)
```

Dopo `auto_generate` (riga 55) aggiungere:
```python
    # If False, this is a scraping-only campaign: no DM workers, no AI generation.
    messaging_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Per-campaign override of SCRAPE_DAILY_LIMIT (lookups/day/account). NULL = use .env default.
    scrape_daily_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

- [ ] **Step 5: Aggiungere la colonna al modello `InstagramAccount`**

In `backend/app/models/account.py`, dopo `daily_message_count` (riga 29) aggiungere:
```python
    scrape_lookups_today: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
```

- [ ] **Step 6: Verificare che i modelli importino senza errori**

Run: `cd backend && ./venv/Scripts/activate && python -c "from app.models.follower import Follower; from app.models.global_contact import GlobalContact; from app.models.campaign import Campaign; from app.models.account import InstagramAccount; print('models ok')"`
Expected: stampa `models ok`, nessuna eccezione.

- [ ] **Step 7: Commit**

```bash
git add backend/alembic/versions/014_advanced_scraping_contacts.py backend/app/models/
git commit -m "feat(db): migration 014 — contact columns, messaging toggle, scrape cap"
```

> **Nota migrazione su Supabase:** `python -m scripts.migrate` va eseguito a bot fermo (vedi CLAUDE.md su lock `idle in transaction`). La migrazione vera si applica in Task 14 (handoff operatore), non durante lo sviluppo locale.

---

## Task 2: Modulo `contact_extract.py` (estrazione pura)

**Files:**
- Create: `backend/app/utils/contact_extract.py`
- Test: `backend/tests/test_contact_extract.py`

- [ ] **Step 1: Scrivere i test (falliscono)**

Create `backend/tests/test_contact_extract.py`:

```python
from app.utils.contact_extract import (
    extract_contacts, ContactData, CONTACT_SOURCE_IG, CONTACT_SOURCE_REGEX,
)


class _Link:
    def __init__(self, url, title=None):
        self.url = url
        self.title = title


class _User:
    """Mirror dei campi instagrapi.types.User usati da extract_contacts."""
    def __init__(self, **kw):
        self.biography = kw.get("biography")
        self.public_email = kw.get("public_email")
        self.public_phone_number = kw.get("public_phone_number")
        self.public_phone_country_code = kw.get("public_phone_country_code")
        self.contact_phone_number = kw.get("contact_phone_number")
        self.external_url = kw.get("external_url")
        self.bio_links = kw.get("bio_links", [])


def test_business_full_contact():
    u = _User(
        biography="Negozio abbigliamento",
        public_email="Shop@Example.COM",
        public_phone_number="3331234567",
        public_phone_country_code="39",
        bio_links=[_Link("https://shop.example.com", "Sito")],
        external_url="https://shop.example.com",
    )
    c = extract_contacts(u)
    assert c.email == "shop@example.com"
    assert c.phone == "+393331234567"
    assert c.sources["email"] == CONTACT_SOURCE_IG
    assert c.sources["phone"] == CONTACT_SOURCE_IG
    assert {l["url"] for l in c.bio_links} == {"https://shop.example.com"}


def test_email_from_bio_text_when_no_business_field():
    u = _User(biography="Scrivimi a info@negozio.it per ordini")
    c = extract_contacts(u)
    assert c.email == "info@negozio.it"
    assert c.sources["email"] == CONTACT_SOURCE_REGEX


def test_whatsapp_link_in_bio_links():
    u = _User(
        biography="Ordina su WhatsApp",
        bio_links=[_Link("https://wa.me/393339998877")],
    )
    c = extract_contacts(u)
    assert c.whatsapp == "+393339998877"
    # whatsapp number also fills phone when phone missing
    assert c.phone == "+393339998877"


def test_phone_in_bio_text():
    u = _User(biography="Chiama +39 333 444 5566 dalle 9 alle 18")
    c = extract_contacts(u)
    assert c.phone == "+393334445566"
    assert c.sources["phone"] == CONTACT_SOURCE_REGEX


def test_no_false_phone_from_year_or_short_number():
    u = _User(biography="Dal 2024 a Milano. Sconto 20%")
    c = extract_contacts(u)
    assert c.phone is None


def test_multiple_bio_links_preserved_and_deduped():
    u = _User(bio_links=[
        _Link("https://a.com", "A"),
        _Link("https://b.com", "B"),
        _Link("https://a.com", "A dup"),
    ], external_url="https://a.com")
    c = extract_contacts(u)
    urls = [l["url"] for l in c.bio_links]
    assert urls.count("https://a.com") == 1
    assert "https://b.com" in urls


def test_business_phone_wins_over_bio_regex():
    u = _User(
        biography="vecchio numero 02 1111111",
        public_phone_number="3331234567",
        public_phone_country_code="39",
    )
    c = extract_contacts(u)
    assert c.phone == "+393331234567"
    assert c.sources["phone"] == CONTACT_SOURCE_IG


def test_empty_input_no_exception():
    c = extract_contacts(_User())
    assert isinstance(c, ContactData)
    assert c.phone is None and c.email is None and c.bio_links == []


def test_handles_none_gracefully():
    c = extract_contacts(None)
    assert isinstance(c, ContactData)
    assert c.phone is None
```

- [ ] **Step 2: Eseguire i test per verificare il fallimento**

Run: `cd backend && ./venv/Scripts/activate && python -m pytest tests/test_contact_extract.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.utils.contact_extract'`.

- [ ] **Step 3: Implementare `contact_extract.py`**

Create `backend/app/utils/contact_extract.py`:

```python
"""Pure contact extraction from an instagrapi user_info object.

Single source of truth used by both the scraper and the import resolver, so
the two paths never diverge (CLAUDE.md anti-divergence rule). No DB/network.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

CONTACT_SOURCE_IG = "ig_business"
CONTACT_SOURCE_REGEX = "bio_regex"
CONTACT_SOURCE_WEBSITE = "website"  # reserved for Fase 2

# Priority for cross-source merges (higher wins).
SOURCE_PRIORITY = {CONTACT_SOURCE_IG: 3, CONTACT_SOURCE_REGEX: 2, CONTACT_SOURCE_WEBSITE: 1}

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Phone candidate: optional +, then digits/separators, 8+ chars, ends on a digit.
_PHONE_RE = re.compile(r"(?<![\w.])(\+?\d[\d\s().\-/]{6,}\d)(?![\w])")
_WA_NUM_RE = re.compile(r"(?:wa\.me/|api\.whatsapp\.com/send\?phone=)\+?(\d{6,15})", re.I)
_WA_GROUP_RE = re.compile(r"(https?://chat\.whatsapp\.com/[A-Za-z0-9]+)", re.I)


@dataclass
class ContactData:
    phone: str | None = None
    email: str | None = None
    whatsapp: str | None = None
    bio_links: list[dict] = field(default_factory=list)
    external_url: str | None = None
    sources: dict = field(default_factory=dict)


def _normalize_email(raw: str | None) -> str | None:
    if not raw:
        return None
    s = str(raw).strip().lower()
    return s if _EMAIL_RE.fullmatch(s) else (s if "@" in s and "." in s else None)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _normalize_phone(raw: str | None) -> str | None:
    """Return E.164-ish '+<digits>' or '<digits>'. None if implausible length."""
    if not raw:
        return None
    s = str(raw).strip()
    plus = s.startswith("+")
    d = _digits(s)
    if not (9 <= len(d) <= 15):
        return None
    return ("+" + d) if plus else d


def _compose_business_phone(number: str | None, cc: str | None) -> str | None:
    num_d = _digits(number or "")
    if not num_d:
        return None
    cc_d = _digits(cc or "")
    if cc_d:
        return "+" + cc_d + num_d.lstrip("0")
    # No country code: accept the number as-is if plausible length.
    return _normalize_phone(num_d)


def _emails_from_text(text: str | None) -> str | None:
    if not text:
        return None
    m = _EMAIL_RE.search(text)
    return m.group(0).lower() if m else None


def _phones_from_text(text: str | None) -> str | None:
    if not text:
        return None
    for m in _PHONE_RE.finditer(text):
        normalized = _normalize_phone(m.group(1))
        if normalized:
            return normalized
    return None


def _whatsapp_from(texts: list[str]) -> tuple[str | None, str | None]:
    """Return (whatsapp_value, phone_from_whatsapp)."""
    for t in texts:
        if not t:
            continue
        m = _WA_NUM_RE.search(t)
        if m:
            phone = _normalize_phone(m.group(1)) or ("+" + _digits(m.group(1)))
            return phone, phone
        g = _WA_GROUP_RE.search(t)
        if g:
            return g.group(1), None
    return None, None


def _bio_links_from(info) -> tuple[list[dict], str | None]:
    links: list[dict] = []
    seen: set[str] = set()

    def _add(url, title):
        if not url:
            return
        u = str(url).strip()
        if not u or u in seen:
            return
        seen.add(u)
        links.append({"url": u, "title": (str(title).strip() if title else None)})

    raw_links = getattr(info, "bio_links", None) or []
    for bl in raw_links:
        if isinstance(bl, dict):
            _add(bl.get("url"), bl.get("title") or bl.get("link_type"))
        else:
            _add(getattr(bl, "url", None), getattr(bl, "title", None))

    external = getattr(info, "external_url", None)
    external = str(external) if external else None
    if external:
        _add(external, None)
    return links, external


def extract_contacts(info) -> ContactData:
    """Extract contacts from an instagrapi User (or None). Never raises."""
    data = ContactData()
    if info is None:
        return data
    try:
        bio = getattr(info, "biography", None) or ""
        bio_links, external = _bio_links_from(info)
        data.bio_links = bio_links
        data.external_url = external
        link_urls = [l["url"] for l in bio_links]

        # ── Email ──
        biz_email = _normalize_email(getattr(info, "public_email", None))
        if biz_email:
            data.email = biz_email
            data.sources["email"] = CONTACT_SOURCE_IG
        else:
            re_email = _emails_from_text(bio)
            if re_email:
                data.email = re_email
                data.sources["email"] = CONTACT_SOURCE_REGEX

        # ── WhatsApp (links + bio text) ──
        wa, wa_phone = _whatsapp_from(link_urls + [bio])
        if wa:
            data.whatsapp = wa
            data.sources["whatsapp"] = CONTACT_SOURCE_REGEX

        # ── Phone ──
        biz_phone = _compose_business_phone(
            getattr(info, "public_phone_number", None)
            or getattr(info, "contact_phone_number", None),
            getattr(info, "public_phone_country_code", None),
        )
        if biz_phone:
            data.phone = biz_phone
            data.sources["phone"] = CONTACT_SOURCE_IG
        elif wa_phone:
            data.phone = wa_phone
            data.sources["phone"] = CONTACT_SOURCE_REGEX
        else:
            re_phone = _phones_from_text(bio)
            if re_phone:
                data.phone = re_phone
                data.sources["phone"] = CONTACT_SOURCE_REGEX
    except Exception:
        # Defensive: bad input must never break a scrape.
        return data
    return data
```

- [ ] **Step 4: Eseguire i test e verificare il passaggio**

Run: `cd backend && ./venv/Scripts/activate && python -m pytest tests/test_contact_extract.py -v`
Expected: PASS (9 test).

- [ ] **Step 5: Commit**

```bash
git add backend/app/utils/contact_extract.py backend/tests/test_contact_extract.py
git commit -m "feat(scrape): pure contact extraction (ig business + bio regex + whatsapp)"
```

---

## Task 3: `global_contact_service` — merge + upsert lead

**Files:**
- Create: `backend/app/services/global_contact_service.py`
- Test: `backend/tests/test_global_contact_merge.py`

- [ ] **Step 1: Scrivere i test puri di merge (falliscono)**

Create `backend/tests/test_global_contact_merge.py`:

```python
from app.services.global_contact_service import merge_scalar, merge_bio_links, merge_scrape_sources
from app.utils.contact_extract import CONTACT_SOURCE_IG, CONTACT_SOURCE_REGEX


def test_merge_scalar_fills_empty():
    val, src = merge_scalar(None, None, "+39333", CONTACT_SOURCE_REGEX)
    assert val == "+39333" and src == CONTACT_SOURCE_REGEX


def test_merge_scalar_keeps_existing_same_priority():
    val, src = merge_scalar("+39111", CONTACT_SOURCE_REGEX, "+39222", CONTACT_SOURCE_REGEX)
    assert val == "+39111" and src == CONTACT_SOURCE_REGEX


def test_merge_scalar_higher_priority_overrides():
    val, src = merge_scalar("+39111", CONTACT_SOURCE_REGEX, "+39222", CONTACT_SOURCE_IG)
    assert val == "+39222" and src == CONTACT_SOURCE_IG


def test_merge_scalar_new_none_keeps_existing():
    val, src = merge_scalar("+39111", CONTACT_SOURCE_IG, None, None)
    assert val == "+39111" and src == CONTACT_SOURCE_IG


def test_merge_bio_links_union_dedup():
    existing = [{"url": "https://a.com", "title": "A"}]
    new = [{"url": "https://a.com", "title": "A2"}, {"url": "https://b.com", "title": "B"}]
    merged = merge_bio_links(existing, new)
    urls = [l["url"] for l in merged]
    assert urls == ["https://a.com", "https://b.com"]


def test_merge_scrape_sources_appends_unique():
    existing = [{"campaign_id": "c1", "scraping_account_id": "a1"}]
    new_entry = {"campaign_id": "c1", "scraping_account_id": "a1"}
    assert merge_scrape_sources(existing, new_entry) == existing  # no dup
    other = {"campaign_id": "c2", "scraping_account_id": "a1"}
    assert len(merge_scrape_sources(existing, other)) == 2
```

- [ ] **Step 2: Eseguire i test per verificare il fallimento**

Run: `cd backend && ./venv/Scripts/activate && python -m pytest tests/test_global_contact_merge.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.global_contact_service'`.

- [ ] **Step 3: Implementare `global_contact_service.py`**

Create `backend/app/services/global_contact_service.py`:

```python
"""Upsert + merge of scraped profiles into the global lead registry.

A scraped profile becomes a "lead visto" in global_contacts even when the
campaign never sends a DM (messaging disabled). The DM dedup stays at send-time
(campaign_orchestrator._mark_globally_contacted) and is unaffected.
"""
from __future__ import annotations

import json
from datetime import datetime

from loguru import logger
from sqlalchemy import select

from app.models.global_contact import GlobalContact
from app.utils.contact_extract import ContactData, SOURCE_PRIORITY


def merge_scalar(existing_val, existing_src, new_val, new_src):
    """Pick the better of two contact values by source priority."""
    if not new_val:
        return existing_val, existing_src
    if not existing_val:
        return new_val, new_src
    if SOURCE_PRIORITY.get(new_src, 0) > SOURCE_PRIORITY.get(existing_src, 0):
        return new_val, new_src
    return existing_val, existing_src


def merge_bio_links(existing: list[dict], new: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for link in (existing or []) + (new or []):
        url = (link or {}).get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "title": link.get("title")})
    return out


def merge_scrape_sources(existing: list[dict], new_entry: dict) -> list[dict]:
    out = list(existing or [])
    for e in out:
        if (e.get("campaign_id") == new_entry.get("campaign_id")
                and e.get("scraping_account_id") == new_entry.get("scraping_account_id")):
            return out
    out.append(new_entry)
    return out


def _load_json(raw, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


async def upsert_lead(
    db,
    *,
    ig_user_id: int,
    username: str | None,
    full_name: str | None,
    biography: str | None,
    contacts: ContactData,
    campaign,
    account,
) -> None:
    """Insert/merge a scraped profile as a lead. Best-effort; never raises fatally."""
    try:
        now = datetime.utcnow()
        source_entry = {
            "campaign_id": campaign.id,
            "campaign_name": campaign.name,
            "scraping_account_id": account.id if account else None,
            "scraping_account_username": account.username if account else None,
            "scraped_at": now.isoformat(),
        }
        contact = (await db.execute(
            select(GlobalContact).where(GlobalContact.ig_user_id == ig_user_id)
        )).scalar_one_or_none()

        if contact is None:
            db.add(GlobalContact(
                ig_user_id=ig_user_id,
                username=username,
                full_name=full_name,
                biography=biography,
                phone=contacts.phone,
                email=contacts.email,
                whatsapp=contacts.whatsapp,
                external_url=contacts.external_url,
                bio_links=json.dumps(contacts.bio_links) if contacts.bio_links else None,
                contact_source=json.dumps(contacts.sources) if contacts.sources else None,
                scrape_sources=json.dumps([source_entry]),
                first_seen_at=now,
                last_contacted_at=None,
                contacted_by_campaign_ids="[]",
                contact_history="[]",
            ))
            await db.commit()
            return

        # Merge into existing
        prev_src = _load_json(contact.contact_source, {})
        for field_name in ("phone", "email", "whatsapp", "external_url"):
            new_val = getattr(contacts, field_name, None)
            new_src = contacts.sources.get(field_name) if field_name != "external_url" else "ig_business"
            merged_val, merged_src = merge_scalar(
                getattr(contact, field_name), prev_src.get(field_name), new_val, new_src,
            )
            setattr(contact, field_name, merged_val)
            if merged_src:
                prev_src[field_name] = merged_src
        contact.contact_source = json.dumps(prev_src) if prev_src else None
        contact.bio_links = json.dumps(
            merge_bio_links(_load_json(contact.bio_links, []), contacts.bio_links)
        )
        contact.scrape_sources = json.dumps(
            merge_scrape_sources(_load_json(contact.scrape_sources, []), source_entry)
        )
        if username:
            contact.username = username
        if full_name:
            contact.full_name = full_name
        if biography:
            contact.biography = biography
        if contact.first_seen_at is None:
            contact.first_seen_at = now
        await db.commit()
    except Exception as e:
        logger.warning(f"[Lead] upsert_lead failed for {ig_user_id} (non-fatal): {e}")
        try:
            await db.rollback()
        except Exception:
            pass
```

- [ ] **Step 4: Eseguire i test e verificare il passaggio**

Run: `cd backend && ./venv/Scripts/activate && python -m pytest tests/test_global_contact_merge.py -v`
Expected: PASS (6 test).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/global_contact_service.py backend/tests/test_global_contact_merge.py
git commit -m "feat(leads): global_contacts upsert + cross-campaign contact merge"
```

---

## Task 4: Cap scraping per-account (`account_manager` + config + exception)

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/app/utils/exceptions.py`
- Modify: `backend/app/services/account_manager.py`
- Modify: `backend/.env.example`
- Test: `backend/tests/test_scrape_cap.py`

- [ ] **Step 1: Aggiungere la setting di config**

In `backend/app/config.py`, dopo `default_daily_limit: int = 20` (riga 82) aggiungere:
```python
    # Max user_info lookups/day/account for scraping (anti-ban). Per-campaign override on campaigns.scrape_daily_limit.
    scrape_daily_limit: int = 180
```

In `backend/.env.example` aggiungere (vicino a `DEFAULT_DAILY_LIMIT`):
```
# Cap anti-ban: lookup user_info/giorno per account durante lo scraping (override per-campagna)
SCRAPE_DAILY_LIMIT=180
```

- [ ] **Step 2: Aggiungere l'eccezione**

In `backend/app/utils/exceptions.py`, aggiungere una nuova eccezione (mantenere la gerarchia esistente; se esiste `ScraperError`, ereditare da essa):
```python
class ScrapeBudgetError(Exception):
    """Raised when no scraping account has remaining daily lookup budget."""
```

- [ ] **Step 3: Scrivere i test puri del cap (falliscono)**

Create `backend/tests/test_scrape_cap.py`:

```python
from app.services.account_manager import scrape_daily_limit_for, has_scrape_budget


class _Acct:
    def __init__(self, lookups):
        self.scrape_lookups_today = lookups


class _Camp:
    def __init__(self, override=None):
        self.scrape_daily_limit = override


def test_limit_uses_env_default_when_no_override(monkeypatch):
    from app.services import account_manager
    monkeypatch.setattr(account_manager.settings, "scrape_daily_limit", 180, raising=False)
    assert scrape_daily_limit_for(_Acct(0), _Camp(None)) == 180


def test_limit_uses_campaign_override():
    assert scrape_daily_limit_for(_Acct(0), _Camp(50)) == 50


def test_has_budget_true_below_limit():
    assert has_scrape_budget(_Acct(10), _Camp(50)) is True


def test_has_budget_false_at_limit():
    assert has_scrape_budget(_Acct(50), _Camp(50)) is False


def test_has_budget_false_above_limit():
    assert has_scrape_budget(_Acct(99), _Camp(50)) is False
```

- [ ] **Step 4: Eseguire i test per verificare il fallimento**

Run: `cd backend && ./venv/Scripts/activate && python -m pytest tests/test_scrape_cap.py -v`
Expected: FAIL — `ImportError: cannot import name 'scrape_daily_limit_for'`.

- [ ] **Step 5: Implementare gli helper nel `account_manager.py`**

In `backend/app/services/account_manager.py` assicurarsi che `from app.config import settings` sia importato (lo è già se usa settings; altrimenti aggiungerlo in cima). Aggiungere in fondo al modulo:

```python
def scrape_daily_limit_for(account, campaign) -> int:
    """Effective lookup cap for this account on this campaign."""
    override = getattr(campaign, "scrape_daily_limit", None)
    if override is not None and override > 0:
        return override
    return settings.scrape_daily_limit


def has_scrape_budget(account, campaign) -> bool:
    return (getattr(account, "scrape_lookups_today", 0) or 0) < scrape_daily_limit_for(account, campaign)


async def increment_scrape_lookup(db, account_id: str) -> None:
    """Atomic +1 on the account's daily scrape lookup counter."""
    from sqlalchemy import update
    from app.models.account import InstagramAccount
    await db.execute(
        update(InstagramAccount)
        .where(InstagramAccount.id == account_id)
        .values(scrape_lookups_today=InstagramAccount.scrape_lookups_today + 1)
    )
    await db.commit()
```

- [ ] **Step 6: Eseguire i test e verificare il passaggio**

Run: `cd backend && ./venv/Scripts/activate && python -m pytest tests/test_scrape_cap.py -v`
Expected: PASS (5 test).

- [ ] **Step 7: Commit**

```bash
git add backend/app/config.py backend/app/utils/exceptions.py backend/app/services/account_manager.py backend/.env.example backend/tests/test_scrape_cap.py
git commit -m "feat(scrape): per-account daily lookup cap (config + helpers)"
```

---

## Task 5: Integrazione scraper (`scraper.py`)

**Files:**
- Modify: `backend/app/services/scraper.py`

> Nessun test unit nuovo: lo scraper richiede login IG/DB live. Verifica via import + py_compile. La logica testabile è già coperta da Task 2/3/4.

- [ ] **Step 1: Importare i nuovi helper in cima a `scraper.py`**

Aggiungere agli import del modulo (vicino agli altri `from app...`):
```python
from app.utils.contact_extract import extract_contacts, ContactData
from app.services.global_contact_service import upsert_lead
from app.services.account_manager import has_scrape_budget, increment_scrape_lookup
from app.utils.exceptions import ScrapeBudgetError
```

- [ ] **Step 2: Applicare il cap + estrazione in `_store_followers_batch`**

In `_store_followers_batch`, dentro il `for user_short in followers_batch:` loop, **prima** del blocco `for attempt in range(2):` (riga ~508), inserire il controllo budget e inizializzare `contacts`:

```python
        # Anti-ban scrape cap: ensure current account still has lookup budget.
        await db.refresh(current_account)
        if not has_scrape_budget(current_account, campaign):
            fallback = await _get_fallback_account(db, exclude_id=current_account.id, campaign_id=campaign.id)
            if fallback:
                logger.warning(
                    f"[Scraper] Cap lookup raggiunto per @{current_account.username} "
                    f"({current_account.scrape_lookups_today}) — rotazione → @{fallback.username}"
                )
                current_client = await _login(fallback, db)
                current_account = fallback
                await db.refresh(current_account)
            if not has_scrape_budget(current_account, campaign):
                raise ScrapeBudgetError(
                    "Cap lookup giornaliero raggiunto su tutti gli account scraping disponibili"
                )

        contacts = ContactData()
```

Poi, dentro il `try` del primo attempt, **dopo** `user_info = await asyncio.to_thread(current_client.user_info_v1, user_short.pk)` e le assegnazioni esistenti (`biography = ...` ecc., riga ~514-519), aggiungere:

```python
                contacts = extract_contacts(user_info)
                await increment_scrape_lookup(db, current_account.id)
                current_account.scrape_lookups_today = (current_account.scrape_lookups_today or 0) + 1
```

(Il secondo assegnamento tiene il valore in memoria allineato per il check budget del profilo successivo senza un refresh extra.)

- [ ] **Step 3: Popolare i campi contatto del `Follower`**

Modificare la costruzione di `Follower(...)` (riga ~567-580) aggiungendo i campi contatto. Sostituire il blocco con:

```python
        follower = Follower(
            campaign_id=campaign.id,
            ig_user_id=user_short.pk,
            username=user_short.username,
            full_name=user_short.full_name,
            biography=biography,
            is_private=user_short.is_private,
            is_verified=is_verified,
            follower_count=follower_count,
            following_count=following_count,
            external_url=contacts.external_url or external_url,
            profile_pic_url=str(user_short.profile_pic_url) if user_short.profile_pic_url else None,
            phone=contacts.phone,
            email=contacts.email,
            whatsapp=contacts.whatsapp,
            bio_links=json.dumps(contacts.bio_links) if contacts.bio_links else None,
            contact_source=json.dumps(contacts.sources) if contacts.sources else None,
            status=FollowerStatus.bio_scraped,
        )
        db.add(follower)
        # Commit per follower — keeps write lock window to milliseconds
        await db.commit()
        stored += 1

        # Register the scraped profile as a "lead visto" in the global registry.
        await upsert_lead(
            db,
            ig_user_id=user_short.pk,
            username=user_short.username,
            full_name=user_short.full_name,
            biography=biography,
            contacts=contacts,
            campaign=campaign,
            account=current_account,
        )
```

(`json` è già importato in `scraper.py`.)

- [ ] **Step 4: Gestire `ScrapeBudgetError` in `_scrape_paginated`**

In `_scrape_paginated`, la chiamata `_store_followers_batch` è dentro il `try` del `while True`. Aggiungere un `except` dedicato **prima** di `except SoftBlockError` (riga ~391):

```python
        except ScrapeBudgetError as e:
            logger.warning(f"[Scraper] {e} — scraping in pausa fino al reset giornaliero.")
            return total, "scrape_capped"
```

- [ ] **Step 5: Gestire l'esito `scrape_capped` in `scrape_followers`**

In `scrape_followers`, dove si gestisce `scrape_outcome == "rate_limited"` (riga ~143), estendere la condizione per trattare `scrape_capped` allo stesso modo (pausa + cursore salvato). Cambiare:
```python
            if scrape_outcome == "rate_limited":
```
in:
```python
            if scrape_outcome in ("rate_limited", "scrape_capped"):
```
e nel corpo, rendere il messaggio evento condizionale:
```python
                campaign.status = CampaignStatus.paused
                campaign.total_followers = actual_count
                campaign.scrape_outcome = scrape_outcome
                campaign.updated_at = datetime.utcnow()
                await db.commit()
                msg = ("Scraping in pausa: cap lookup giornaliero raggiunto — riprende dopo il reset"
                       if scrape_outcome == "scrape_capped"
                       else "Scraping interrotto da rate limit ripetuti — ripristinabile (cursore salvato)")
                emit_event(campaign_id, "scrape_stopped", msg, level="warn" if scrape_outcome == "scrape_capped" else "error")
                return
```

- [ ] **Step 6: Applicare il toggle messaggistica allo stato finale**

In `scrape_followers`, nel blocco finale (riga ~161-166) sostituire:
```python
            if campaign.status == CampaignStatus.scraping_and_running:
                campaign.status = CampaignStatus.running
            else:
                campaign.status = CampaignStatus.ready
```
con:
```python
            if campaign.status == CampaignStatus.scraping_and_running:
                campaign.status = CampaignStatus.running
            elif not campaign.messaging_enabled:
                campaign.status = CampaignStatus.completed
            else:
                campaign.status = CampaignStatus.ready
```

- [ ] **Step 7: Verificare compilazione e import**

Run: `cd backend && ./venv/Scripts/activate && python -m py_compile app/services/scraper.py && python -c "import app.services.scraper; print('scraper ok')"`
Expected: stampa `scraper ok`, nessun errore.

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/scraper.py
git commit -m "feat(scrape): extract contacts + upsert lead + per-account lookup cap"
```

---

## Task 6: Integrazione import resolver (`import_resolver.py`)

**Files:**
- Modify: `backend/app/services/import_resolver.py`

- [ ] **Step 1: Importare i nuovi helper**

In cima a `import_resolver.py` aggiungere:
```python
from app.utils.contact_extract import extract_contacts
from app.services.global_contact_service import upsert_lead
from app.services.account_manager import has_scrape_budget, increment_scrape_lookup
```

- [ ] **Step 2: Cap + incremento lookup nel loop di `resolve_imports`**

In `resolve_imports`, dentro il `while True:` loop, **dopo** `row = (...).scalar_one_or_none()` e il check `if row is None: break` (riga ~160), e **prima** di `info, err, client, account = await _resolve_one(...)`, inserire:

```python
                await db.refresh(account)
                if not has_scrape_budget(account, campaign):
                    fb = await _get_fallback_account(db, exclude_id=account.id, campaign_id=campaign.id)
                    if fb:
                        logger.warning(f"[Import] Cap lookup per @{account.username} — rotazione → @{fb.username}")
                        await release_scraping_slot(account.id)
                        account = fb
                        await acquire_scraping_slot(account.id)
                        acct_id = account.id
                        client = await _login(account, db)
                        await db.refresh(account)
                    if not has_scrape_budget(account, campaign):
                        campaign.status = CampaignStatus.paused
                        campaign.scrape_outcome = "scrape_capped"
                        campaign.updated_at = datetime.utcnow()
                        await db.commit()
                        emit_event(campaign_id, "scrape_stopped",
                                   "Risoluzione in pausa: cap lookup giornaliero raggiunto — riprende dopo il reset",
                                   level="warn")
                        return
```

- [ ] **Step 3: Incremento lookup dopo risoluzione riuscita**

In `resolve_imports`, subito **dopo** `info, err, client, account = await _resolve_one(...)` (riga ~163), aggiungere:
```python
                if info is not None:
                    await increment_scrape_lookup(db, account.id)
                    account.scrape_lookups_today = (account.scrape_lookups_today or 0) + 1
```

- [ ] **Step 4: Estrazione contatti + campi Follower + upsert lead**

Nel blocco `if dup is None:` (riga ~172-188) sostituire la creazione `Follower(...)` con la versione arricchita e aggiungere l'upsert. Sostituire:
```python
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
```
con:
```python
                    if dup is None:
                        contacts = extract_contacts(info)
                        biography = getattr(info, "biography", None) or None
                        db.add(Follower(
                            campaign_id=campaign_id,
                            ig_user_id=info.pk,
                            username=info.username,
                            full_name=getattr(info, "full_name", None),
                            biography=biography,
                            is_private=getattr(info, "is_private", False),
                            is_verified=getattr(info, "is_verified", False),
                            follower_count=getattr(info, "follower_count", None),
                            following_count=getattr(info, "following_count", None),
                            external_url=contacts.external_url,
                            profile_pic_url=str(info.profile_pic_url) if getattr(info, "profile_pic_url", None) else None,
                            phone=contacts.phone,
                            email=contacts.email,
                            whatsapp=contacts.whatsapp,
                            bio_links=json.dumps(contacts.bio_links) if contacts.bio_links else None,
                            contact_source=json.dumps(contacts.sources) if contacts.sources else None,
                            status=FollowerStatus.bio_scraped,
                        ))
                        resolved += 1
                        await db.commit()
                        await upsert_lead(
                            db,
                            ig_user_id=info.pk,
                            username=info.username,
                            full_name=getattr(info, "full_name", None),
                            biography=biography,
                            contacts=contacts,
                            campaign=campaign,
                            account=account,
                        )
```

(`json` è già importato in `import_resolver.py`. Il `await db.commit()` esistente più sotto resta: committa lo `status` della riga import; il commit aggiunto qui rende il follower visibile prima dell'upsert lead.)

- [ ] **Step 5: Applicare il toggle messaggistica allo stato finale**

In `resolve_imports`, nel blocco "Completato" (riga ~234-237) sostituire:
```python
            if campaign.status == CampaignStatus.scraping_and_running:
                campaign.status = CampaignStatus.running
            elif campaign.status in _RESOLVING:
                campaign.status = CampaignStatus.ready
```
con:
```python
            if campaign.status == CampaignStatus.scraping_and_running:
                campaign.status = CampaignStatus.running
            elif campaign.status in _RESOLVING:
                campaign.status = CampaignStatus.completed if not campaign.messaging_enabled else CampaignStatus.ready
```

- [ ] **Step 6: Verificare compilazione e import**

Run: `cd backend && ./venv/Scripts/activate && python -m py_compile app/services/import_resolver.py && python -c "import app.services.import_resolver; print('resolver ok')" && python -m pytest tests/test_import_resolver.py -v`
Expected: stampa `resolver ok` + i 4 test classifier ancora PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/import_resolver.py
git commit -m "feat(import): extract contacts + upsert lead + lookup cap in resolver"
```

---

## Task 7: Merge contatti a send-time (`campaign_orchestrator.py`)

**Files:**
- Modify: `backend/app/services/campaign_orchestrator.py`

> Obiettivo: quando un DM va a buon fine e si marca `global_contacts`, aggiornare anche i campi contatto se più completi (stesso merge dello scrape). Non deve introdurre regressioni nella dedup invio.

- [ ] **Step 1: Arricchire `_mark_globally_contacted` con i campi contatto del follower**

In `_mark_globally_contacted` (riga ~1349-1397), nel ramo `if contact:` dopo le assegnazioni `username/full_name/biography` (riga ~1381-1385) aggiungere il merge dei campi contatto dal follower:

```python
        if follower:
            contact.username = follower.username
            if follower.full_name:
                contact.full_name = follower.full_name
            if follower.biography:
                contact.biography = follower.biography
            # Merge contact fields (fill gaps; don't clobber existing values).
            for field_name in ("phone", "email", "whatsapp", "external_url"):
                fv = getattr(follower, field_name, None)
                if fv and not getattr(contact, field_name, None):
                    setattr(contact, field_name, fv)
            if getattr(follower, "bio_links", None) and not contact.bio_links:
                contact.bio_links = follower.bio_links
```

E nel ramo `else:` (creazione nuovo `GlobalContact`, riga ~1387-1395) aggiungere i campi contatto:

```python
        contact = GlobalContact(
            ig_user_id=ig_user_id,
            username=follower.username if follower else None,
            full_name=follower.full_name if follower else None,
            biography=follower.biography if follower else None,
            phone=getattr(follower, "phone", None) if follower else None,
            email=getattr(follower, "email", None) if follower else None,
            whatsapp=getattr(follower, "whatsapp", None) if follower else None,
            external_url=getattr(follower, "external_url", None) if follower else None,
            bio_links=getattr(follower, "bio_links", None) if follower else None,
            last_contacted_at=now,
            first_seen_at=now,
            contacted_by_campaign_ids=json.dumps([campaign_id]),
            contact_history=json.dumps([history_entry]),
            scrape_sources="[]",
        )
        db.add(contact)
```

- [ ] **Step 2: Verificare compilazione e import**

Run: `cd backend && ./venv/Scripts/activate && python -m py_compile app/services/campaign_orchestrator.py && python -c "import app.services.campaign_orchestrator; print('orch ok')"`
Expected: stampa `orch ok`.

- [ ] **Step 3: Eseguire i test orchestrator esistenti (no regressione)**

Run: `cd backend && ./venv/Scripts/activate && python -m pytest tests/test_campaign_orchestrator.py tests/test_orchestrator_with_fakes.py -v`
Expected: PASS (stesso numero di prima).

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/campaign_orchestrator.py
git commit -m "feat(leads): merge contact fields into global_contacts at send-time"
```

---

## Task 8: Reset contatore scraping nel `daily_reset`

**Files:**
- Modify: `backend/app/workers/task_queue.py`

- [ ] **Step 1: Resettare `scrape_lookups_today` nel cron**

In `backend/app/workers/task_queue.py`, dentro `daily_reset`, alla riga 111 sostituire:
```python
        await db.execute(update(InstagramAccount).values(daily_message_count=0))
```
con:
```python
        await db.execute(update(InstagramAccount).values(daily_message_count=0, scrape_lookups_today=0))
```

E aggiornare il docstring (riga 94) aggiungendo: `Reset daily_message_count and scrape_lookups_today for all accounts.`

- [ ] **Step 2: Verificare compilazione**

Run: `cd backend && ./venv/Scripts/activate && python -m py_compile app/workers/task_queue.py && python -c "import app.workers.task_queue; print('tq ok')"`
Expected: stampa `tq ok`.

- [ ] **Step 3: Commit**

```bash
git add backend/app/workers/task_queue.py
git commit -m "feat(scrape): reset daily scrape lookup counter in daily_reset cron"
```

---

## Task 9: API campagne — toggle, scrape_daily_limit, guard, schema

**Files:**
- Modify: `backend/app/schemas/campaign.py`
- Modify: `backend/app/api/campaigns.py`
- Test: `backend/tests/test_campaign_messaging_toggle.py`

- [ ] **Step 1: Scrivere il test sullo schema (fallisce)**

Create `backend/tests/test_campaign_messaging_toggle.py`:

```python
import pytest
from pydantic import ValidationError
from app.schemas.campaign import CampaignCreate


def test_messaging_enabled_requires_template():
    with pytest.raises(ValidationError):
        CampaignCreate(name="x", target_username="shop", messaging_enabled=True,
                       base_message_template=None)


def test_messaging_disabled_allows_empty_template():
    c = CampaignCreate(name="x", target_username="shop", messaging_enabled=False)
    assert c.messaging_enabled is False
    assert c.base_message_template in (None, "")


def test_messaging_enabled_with_template_ok():
    c = CampaignCreate(name="x", target_username="shop", messaging_enabled=True,
                       base_message_template="Ciao {username}, ti scrivo per...")
    assert c.messaging_enabled is True


def test_default_messaging_enabled_true_requires_template():
    # Backward compat: omitting messaging_enabled defaults to True → template required.
    with pytest.raises(ValidationError):
        CampaignCreate(name="x", target_username="shop")
```

- [ ] **Step 2: Eseguire il test per verificare il fallimento**

Run: `cd backend && ./venv/Scripts/activate && python -m pytest tests/test_campaign_messaging_toggle.py -v`
Expected: FAIL (campo `messaging_enabled` inesistente / template ancora obbligatorio).

- [ ] **Step 3: Aggiornare gli schemi `campaign.py`**

In `backend/app/schemas/campaign.py`:

In `CampaignCreate` cambiare `base_message_template` (riga 10) da:
```python
    base_message_template: str = Field(..., min_length=10)
```
a:
```python
    base_message_template: str | None = Field(default=None)
    messaging_enabled: bool = True
    scrape_daily_limit: int | None = Field(default=None, ge=1, le=2000)
```

E aggiornare il validator `_check_source` (riga 28-32) per validare anche il template:
```python
    @model_validator(mode='after')
    def _check_source(self):
        if self.source_type == 'scrape' and not (self.target_username and self.target_username.strip()):
            raise ValueError("target_username obbligatorio per source_type='scrape'")
        if self.messaging_enabled:
            t = (self.base_message_template or "").strip()
            if len(t) < 10:
                raise ValueError("base_message_template obbligatorio (min 10 caratteri) quando messaging_enabled=True")
        return self
```

In `CampaignUpdate` cambiare `base_message_template` (riga 37) da `min_length=10` a opzionale senza min, e aggiungere i due campi:
```python
    base_message_template: str | None = None
    messaging_enabled: bool | None = None
    scrape_daily_limit: int | None = Field(default=None, ge=1, le=2000)
```

In `CampaignResponse` cambiare `base_message_template: str` (riga 60) in `base_message_template: str | None` e aggiungere dopo `auto_generate` (riga 89):
```python
    messaging_enabled: bool = True
    scrape_daily_limit: int | None = None
```

- [ ] **Step 4: Eseguire il test schema e verificare il passaggio**

Run: `cd backend && ./venv/Scripts/activate && python -m pytest tests/test_campaign_messaging_toggle.py -v`
Expected: PASS (4 test).

- [ ] **Step 5: Persistere i nuovi campi in `create_campaign`**

In `backend/app/api/campaigns.py`, in `create_campaign` (riga 123-140), aggiungere al costruttore `Campaign(...)`:
```python
        messaging_enabled=data.messaging_enabled,
        scrape_daily_limit=data.scrape_daily_limit,
```
e cambiare:
```python
        base_message_template=data.base_message_template,
```
in:
```python
        base_message_template=(data.base_message_template or None),
```

- [ ] **Step 6: Gestire i nuovi campi in `update_campaign`**

In `update_campaign` (riga 174-197), cambiare il blocco template:
```python
    if data.base_message_template is not None:
        campaign.base_message_template = data.base_message_template
```
in:
```python
    if "base_message_template" in data.model_fields_set:
        campaign.base_message_template = data.base_message_template or None
    if data.messaging_enabled is not None:
        campaign.messaging_enabled = data.messaging_enabled
    if "scrape_daily_limit" in data.model_fields_set:
        campaign.scrape_daily_limit = data.scrape_daily_limit
```

- [ ] **Step 7: Guard su `start_campaign`**

In `start_campaign` (riga 371), dopo il check status (riga 374-375) aggiungere:
```python
    if not campaign.messaging_enabled:
        raise HTTPException(
            status_code=400,
            detail="Messaggistica disattivata per questa campagna. Attiva 'Invia messaggi' e imposta un template per inviare DM.",
        )
    if not (campaign.base_message_template or "").strip():
        raise HTTPException(
            status_code=400,
            detail="Template messaggio mancante. Imposta il messaggio base prima di avviare l'invio.",
        )
```

- [ ] **Step 8: Guard su `start_dm_auto`**

In `start_dm_auto` (riga 558), subito dopo aver caricato la campagna (`campaign = await _get_or_404(...)`), aggiungere lo stesso guard:
```python
    if not campaign.messaging_enabled:
        raise HTTPException(
            status_code=400,
            detail="Messaggistica disattivata per questa campagna. Attiva 'Invia messaggi' per usare l'invio automatico.",
        )
```

- [ ] **Step 9: Verificare import + suite campagne**

Run: `cd backend && ./venv/Scripts/activate && python -c "import app.api.campaigns; print('api ok')" && python -m pytest tests/test_campaign_messaging_toggle.py tests/test_enqueue_collection.py -v`
Expected: stampa `api ok` + PASS.

- [ ] **Step 10: Commit**

```bash
git add backend/app/schemas/campaign.py backend/app/api/campaigns.py backend/tests/test_campaign_messaging_toggle.py
git commit -m "feat(campaign): optional messaging toggle + scrape_daily_limit + start guards"
```

---

## Task 10: Export lead + filtri multi-select + schema

**Files:**
- Modify: `backend/app/schemas/lead.py`
- Modify: `backend/app/schemas/follower.py`
- Modify: `backend/app/api/leads.py`

- [ ] **Step 1: Estendere lo schema `LeadResponse`**

In `backend/app/schemas/lead.py`, in `LeadResponse` dopo `external_url` (riga 15) aggiungere:
```python
    phone: Optional[str] = None
    email: Optional[str] = None
    whatsapp: Optional[str] = None
    bio_links: List[Any] = []
    scraping_accounts: List[str] = []   # usernames degli account che hanno scrapato il lead
```

- [ ] **Step 2: Estendere lo schema `FollowerResponse`**

In `backend/app/schemas/follower.py`, in `FollowerResponse` dopo `external_url` (riga 18) aggiungere:
```python
    phone: str | None = None
    email: str | None = None
    whatsapp: str | None = None
    bio_links: str | None = None  # JSON string
```

- [ ] **Step 3: Aggiornare `leads.py` — lettura campi contatto da GlobalContact**

In `backend/app/api/leads.py`, in `_row_to_lead` (riga 125-149) aggiungere la lettura dei nuovi campi da `gc` e di `scraping_accounts` da `gc.scrape_sources`:

```python
def _row_to_lead(row) -> LeadResponse:
    gc = row[0]
    try:
        history = json.loads(gc.contact_history) if gc.contact_history else []
    except Exception:
        history = []
    sources_str = row.scrape_sources or ""
    scrape_sources = [s.strip() for s in sources_str.split(",") if s.strip()] if sources_str else []
    try:
        bio_links = json.loads(gc.bio_links) if gc.bio_links else []
    except Exception:
        bio_links = []
    try:
        scrape_src_json = json.loads(gc.scrape_sources) if gc.scrape_sources else []
        scraping_accounts = sorted({
            e.get("scraping_account_username") for e in scrape_src_json
            if e.get("scraping_account_username")
        })
    except Exception:
        scraping_accounts = []
    return LeadResponse(
        ig_user_id=gc.ig_user_id,
        username=gc.username,
        full_name=gc.full_name,
        biography=gc.biography,
        follower_count=row.follower_count,
        following_count=row.following_count,
        is_verified=bool(row.is_verified),
        external_url=gc.external_url or row.external_url,
        profile_pic_url=row.profile_pic_url,
        phone=gc.phone,
        email=gc.email,
        whatsapp=gc.whatsapp,
        bio_links=bio_links,
        scraping_accounts=scraping_accounts,
        contact_history=history,
        contacts_count=len(history),
        scrape_sources=scrape_sources,
        has_replied=bool(row.has_replied),
        last_contacted_at=gc.last_contacted_at,
        created_at=gc.created_at,
    )
```

- [ ] **Step 4: Estendere `_build_conditions` con i nuovi filtri**

In `_build_conditions` (riga 72-113) aggiungere i parametri e le condizioni. Cambiare la firma e il corpo:

```python
def _build_conditions(stats_sq, search, campaign_id, has_replied,
                      verified_only, min_followers, date_from, date_to,
                      campaign_ids=None, scraping_account_ids=None,
                      has_phone=False, has_email=False):
    conditions = []
    if search:
        s = f'%{search}%'
        conditions.append(or_(
            GlobalContact.username.ilike(s),
            GlobalContact.full_name.ilike(s),
            GlobalContact.biography.ilike(s),
        ))
    if campaign_id:
        conditions.append(
            exists(select(1).where(
                Follower.ig_user_id == GlobalContact.ig_user_id,
                Follower.campaign_id == campaign_id,
            ))
        )
    if campaign_ids:
        conditions.append(
            exists(select(1).where(
                Follower.ig_user_id == GlobalContact.ig_user_id,
                Follower.campaign_id.in_(campaign_ids),
            ))
        )
    if scraping_account_ids:
        # scrape_sources is a JSON array of objects containing scraping_account_id.
        conditions.append(or_(*[
            GlobalContact.scrape_sources.like(f'%"{aid}"%') for aid in scraping_account_ids
        ]))
    if has_phone:
        conditions.append(GlobalContact.phone.isnot(None))
    if has_email:
        conditions.append(GlobalContact.email.isnot(None))
    if has_replied is True:
        conditions.append(stats_sq.c.has_replied == 1)
    elif has_replied is False:
        conditions.append(or_(stats_sq.c.has_replied == 0, stats_sq.c.has_replied.is_(None)))
    if verified_only:
        conditions.append(stats_sq.c.is_verified == 1)
    if min_followers is not None:
        conditions.append(stats_sq.c.follower_count >= min_followers)
    if date_from:
        try:
            conditions.append(GlobalContact.last_contacted_at >= datetime.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            conditions.append(GlobalContact.last_contacted_at <= datetime.fromisoformat(date_to))
        except ValueError:
            pass
    return conditions
```

E aggiornare `_filter_args` (riga 116-122) per includere i nuovi campi:
```python
def _filter_args(search, campaign_id, has_replied, verified_only, min_followers, date_from, date_to,
                 campaign_ids=None, scraping_account_ids=None, has_phone=False, has_email=False):
    return dict(
        search=search, campaign_id=campaign_id, has_replied=has_replied,
        verified_only=verified_only, min_followers=min_followers,
        date_from=date_from, date_to=date_to,
        campaign_ids=campaign_ids, scraping_account_ids=scraping_account_ids,
        has_phone=has_phone, has_email=has_email,
    )
```

- [ ] **Step 5: Aggiungere i query param a `list_leads` e `export_leads_csv`**

In `list_leads` (riga 152-166) aggiungere i parametri dopo `date_to`:
```python
    campaign_ids: list[str] | None = Query(default=None, description="Filter by multiple campaign ids"),
    scraping_account_ids: list[str] | None = Query(default=None, description="Filter by scraping account ids"),
    has_phone: bool = Query(default=False, description="Only leads with a phone"),
    has_email: bool = Query(default=False, description="Only leads with an email"),
```
e aggiornare la chiamata `_filter_args(...)` (riga 165) passando i nuovi argomenti:
```python
    fargs = _filter_args(search, campaign_id, has_replied, verified_only,
                         min_followers, date_from, date_to,
                         campaign_ids=campaign_ids, scraping_account_ids=scraping_account_ids,
                         has_phone=has_phone, has_email=has_email)
```

Fare la **stessa** aggiunta di parametri e di `_filter_args` in `export_leads_csv` (riga 251-263).

- [ ] **Step 6: Aggiungere le colonne contatto al CSV**

In `export_leads_csv`, estendere `fieldnames` (riga 291-296) e le righe scritte (riga 301-315). Cambiare `fieldnames` in:
```python
    writer = csv.DictWriter(output, fieldnames=[
        "ig_user_id", "username", "full_name", "biography",
        "follower_count", "following_count", "is_verified",
        "phone", "email", "whatsapp", "external_url", "bio_links",
        "scrape_sources", "scraping_accounts", "contacts_count",
        "has_replied", "last_contacted_at", "created_at",
    ])
```
e nel `writer.writerow({...})` aggiungere le chiavi:
```python
            "phone": lead.phone or "",
            "email": lead.email or "",
            "whatsapp": lead.whatsapp or "",
            "bio_links": " | ".join(l.get("url", "") for l in lead.bio_links),
            "scraping_accounts": ",".join(lead.scraping_accounts),
```
(mantenendo le chiavi esistenti `external_url`, `scrape_sources`, ecc.)

- [ ] **Step 7: Verificare import + test leads esistenti**

Run: `cd backend && ./venv/Scripts/activate && python -c "import app.api.leads; print('leads ok')" && python -m pytest tests/test_leads_queries.py -v`
Expected: stampa `leads ok` + PASS (no regressione).

- [ ] **Step 8: Commit**

```bash
git add backend/app/schemas/lead.py backend/app/schemas/follower.py backend/app/api/leads.py
git commit -m "feat(leads): contact columns in export + multi-select campaign/account + phone/email filters"
```

---

## Task 11: Frontend — tipi, API, toggle nel form campagna

**Files:**
- Modify: `frontend/src/lib/types.ts`
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/components/campaigns/CampaignForm.tsx` (verificare il path esatto con glob)

> Prima di iniziare: `cd frontend` e individuare il form con
> `Glob "src/**/CampaignForm*.tsx"` (path può variare). Seguire i pattern esistenti del file.

- [ ] **Step 1: Estendere i tipi TypeScript**

In `frontend/src/lib/types.ts`, nell'interfaccia `Campaign` aggiungere:
```typescript
  messaging_enabled: boolean;
  scrape_daily_limit: number | null;
  base_message_template: string | null;
```
Nell'interfaccia `Lead` (o `LeadResponse`) aggiungere:
```typescript
  phone: string | null;
  email: string | null;
  whatsapp: string | null;
  bio_links: { url: string; title: string | null }[];
  scraping_accounts: string[];
```
Nell'interfaccia `Follower` (se presente) aggiungere `phone`, `email`, `whatsapp`, `bio_links` (string | null).

- [ ] **Step 2: Estendere il payload di creazione/aggiornamento campagna in `api.ts`**

In `frontend/src/lib/api.ts`, nei tipi/payload usati da `createCampaign`/`updateCampaign` aggiungere i campi `messaging_enabled`, `scrape_daily_limit`, e rendere `base_message_template` opzionale. Nei filtri export leads aggiungere il supporto a `campaign_ids[]`, `scraping_account_ids[]`, `has_phone`, `has_email` (querystring con valori ripetuti per gli array).

Esempio helper querystring per array (se non già presente):
```typescript
function appendArray(params: URLSearchParams, key: string, values?: string[]) {
  (values || []).forEach((v) => params.append(key, v));
}
```

- [ ] **Step 3: Aggiungere il toggle "Invia messaggi" nel form campagna**

Nel componente del form campagna, aggiungere uno switch controllato `messagingEnabled` (default `true`). Quando OFF:
- nascondere/rendere opzionali i campi template (`base_message_template`, `message_template_b`, contesto AI);
- mostrare un hint: "Campagna solo raccolta lead — nessun messaggio verrà inviato".

Includere il valore nel submit (`messaging_enabled: messagingEnabled`) e `base_message_template` come `null`/stringa vuota quando OFF. Aggiungere un campo numerico opzionale `scrape_daily_limit` (label: "Cap lookup/giorno per account (anti-ban)") nella sezione scraping.

Seguire gli stili e i componenti UI già usati nel form (shadcn/ui `Switch`, `Input`, `Label`).

- [ ] **Step 4: Verificare il typecheck**

Run: `cd frontend && npm run build 2>&1 | head -40` (oppure `npx tsc --noEmit`)
Expected: nessun errore di tipo introdotto dalle modifiche.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/types.ts frontend/src/lib/api.ts frontend/src/components/campaigns/
git commit -m "feat(ui): messaging toggle + scrape cap field in campaign form; contact types"
```

---

## Task 12: Frontend — colonne contatto + filtri nella pagina leads

**Files:**
- Modify: `frontend/src/app/leads/page.tsx` (verificare path con glob)

> Individuare la pagina con `Glob "src/**/leads/**/*.tsx"`.

- [ ] **Step 1: Mostrare le colonne contatto**

Nella tabella/lista leads aggiungere le colonne **Telefono**, **Email**, **WhatsApp** e un'icona/lista per i **link bio** (`bio_links`). Mostrare un badge "Non contattato" quando `last_contacted_at` è null. Mantenere lo stile esistente.

- [ ] **Step 2: Filtri multi-select + toggle**

Aggiungere:
- multi-select **Campagne** (popolato da `api.campaigns.list()`), valore → `campaign_ids[]`;
- multi-select **Account scraping** (popolato da `api.accounts.list()`), valore → `scraping_account_ids[]`;
- checkbox **Solo con telefono** → `has_phone`, **Solo con email** → `has_email`.

Passare questi filtri sia alla `list` sia all'**export** (`/leads/export`), così il CSV scaricato rispetta la selezione (requisito: non esportare contatti di altre campagne/clienti).

- [ ] **Step 3: Verificare typecheck/build**

Run: `cd frontend && npm run build 2>&1 | head -40`
Expected: nessun errore introdotto.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/app/leads/
git commit -m "feat(ui): contact columns + multi-select campaign/account filters on leads page"
```

---

## Task 13: Suite completa + verifica finale

**Files:** nessuno (solo verifica)

- [ ] **Step 1: Eseguire l'intera suite backend**

Run: `cd backend && ./venv/Scripts/activate && python -m pytest tests/ -v`
Expected: tutti i test PASS. I nuovi: `test_contact_extract` (9), `test_global_contact_merge` (6), `test_scrape_cap` (5), `test_campaign_messaging_toggle` (4). Nessuna regressione sui preesistenti (il test `test_reservation.py` può richiedere Postgres configurato — se skippa/erra per mancanza DB, è atteso e preesistente, non causato da questo lavoro).

- [ ] **Step 2: Smoke import dell'app**

Run: `cd backend && ./venv/Scripts/activate && python -c "from app.main import app; print('app import ok')"`
Expected: stampa `app import ok`.

- [ ] **Step 3: Build frontend**

Run: `cd frontend && npm run build 2>&1 | tail -20`
Expected: build completata senza errori.

- [ ] **Step 4: Commit (se servono fix emersi)**

```bash
git add -A
git commit -m "test: full suite green for advanced scraping & contacts"
```

---

## Task 14: Documentazione + memoria + handoff migrazione

**Files:**
- Modify: `CLAUDE.md`, `INDEX.md`, `docs/project/PROGRESS.md`
- Modify: `C:\Users\39342\.claude\projects\d--BOT-OUTBOUND\memory\project_state.md`
- Modify: `C:\Users\39342\.claude\projects\d--BOT-OUTBOUND\memory\MEMORY.md`

- [ ] **Step 1: Aggiornare `CLAUDE.md`**

Nella sezione database, documentare le nuove colonne (`followers`/`global_contacts`: phone/email/whatsapp/bio_links/contact_source/contact_extra/scrape_sources/first_seen_at; `campaigns`: messaging_enabled, scrape_daily_limit, template nullable; `instagram_accounts`: scrape_lookups_today). Aggiungere una sottosezione "Scraping avanzato / lead harvesting" con: estrazione contatti (1 call), cap lookup/account (`SCRAPE_DAILY_LIMIT`), toggle messaggistica, numeri operatore (§10.4 della spec). Aggiungere `scrape_daily_limit` alla sezione `.env`.

- [ ] **Step 2: Aggiornare `INDEX.md` e `docs/project/PROGRESS.md`**

In `PROGRESS.md` aggiungere una fase "7F — Scraping avanzato + contatti + messaggistica opzionale ✅". In `INDEX.md` aggiornare lo stato/ordine lettura se elenca le feature.

- [ ] **Step 3: Aggiornare la memoria persistente**

In `project_state.md` aggiungere una sezione datata `## 2026-06-06 — Feature: scraping avanzato + contatti + messaggistica opzionale` con: cosa è stato fatto, file toccati, migrazione 014, decisioni chiave (toggle, merge, cap), comportamento atteso. Verificare `MEMORY.md` (indice) — aggiungere una riga solo se si crea un nuovo file memory (non necessario qui).

- [ ] **Step 4: Applicare la migrazione 014 a Supabase (handoff operatore)**

> **Eseguire solo a bot fermo.** Procedura:
> 1. Fermare backend FastAPI + worker ARQ + cron worker.
> 2. Verificare nessuna sessione `idle in transaction` che locka `campaigns`/`followers` (vedi CLAUDE.md).
> 3. `cd backend && ./venv/Scripts/activate && python -m scripts.migrate`
> 4. Verificare `alembic` head = 014.
> 5. Riavviare backend + worker ARQ + cron worker.

- [ ] **Step 5: Commit finale**

```bash
git add CLAUDE.md INDEX.md docs/project/PROGRESS.md
git commit -m "docs: document advanced scraping, contacts harvesting & messaging toggle"
```

---

## Self-review (eseguita in scrittura)

- **Copertura spec**: §5 dati → Task 1; §6 contact_extract → Task 2; §7 scraper → Task 5; §8 resolver → Task 6; §9 global_contacts merge → Task 3 + Task 7; §10 cap → Task 4/5/6/8; §11 toggle → Task 9 (+ stato finale in Task 5/6); §12 export/filtri → Task 10 + Task 12; frontend → Task 11/12; docs → Task 14. §13 Fase 2 esplicitamente fuori scope.
- **Coerenza tipi**: `ContactData`, `extract_contacts`, `upsert_lead`, `merge_scalar`, `merge_bio_links`, `merge_scrape_sources`, `scrape_daily_limit_for`, `has_scrape_budget`, `increment_scrape_lookup`, `scrape_lookups_today`, `messaging_enabled`, `scrape_daily_limit` — nomi usati in modo identico in tutti i task.
- **No placeholder**: ogni step ha codice/comando completo.
- **Nota deviazione minore vs spec**: aggiunto `contact_source` anche su `global_contacts` (serve al merge per-fonte cross-call) — migliora la regola di merge §9.2, nessun impatto negativo.
```
