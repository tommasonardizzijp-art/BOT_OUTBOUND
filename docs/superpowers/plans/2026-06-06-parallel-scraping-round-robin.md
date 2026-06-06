# Scraping multi-account round-robin per-lead — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Far lavorare tutti gli account scraping di una campagna a turno (round-robin) per-lead, condividendo il carico dall'inizio, in un singolo job seriale.

**Architecture:** Un `ScrapingPool` pre-logga tutti gli account `scraping`/`both` della campagna e li tiene in memoria. Il bio-fetch in `_store_followers_batch` chiede al pool il prossimo account ad ogni lead (round-robin, saltando quelli a cap). Nessun worker parallelo, nessuno stato `pending`, nessun lock, nessun DB nuovo. Il session break resta campagna-level (UI countdown invariato).

**Tech Stack:** Python 3.13, instagrapi, SQLAlchemy async, pytest; frontend Next.js 14 (App Router) + TypeScript.

**Spec:** `docs/superpowers/specs/2026-06-06-parallel-scraping-round-robin-design.md`

---

## File Structure

- **Create** `backend/app/services/scraping_pool.py` — classe `ScrapingPool` (build/next/release/save_sessions) + `ScrapingPoolEmpty`. Unica responsabilità: gestire il pool di client loggati e il round-robin.
- **Modify** `backend/app/services/scraper.py`:
  - estrai helper `_eligible_scraping_accounts(db, campaign_id) -> list[InstagramAccount]` (riusa la query duplicata in `_get_available_account`/`_get_fallback_account`).
  - `scrape_followers` — costruisce il pool, lo passa giù, lo rilascia in `finally`.
  - `_scrape_paginated` — firma `(pool, campaign, db, scrape_mode)`; usa un client del pool per la paginazione, salva le sessioni del pool a ogni batch.
  - `_store_followers_batch` — firma `(followers_batch, campaign, db, pool, scrape_mode)`; round-robin per-lead via `pool.next`.
- **Create** `backend/tests/test_scraping_pool.py` — unit test del round-robin/capped-skip + integrazione `_store_followers_batch` con pool fake.
- **Modify** `frontend/app/campaigns/new/page.tsx` — helper text delay (form nuova campagna).
- **Modify** `frontend/app/campaigns/[id]/page.tsx` — helper text delay (modale impostazioni).

**Comandi (sempre da `backend/` con venv attivo):**
- Attiva venv: `./venv/Scripts/activate` (Windows)
- Test: `python -m pytest tests/test_scraping_pool.py -v`
- Typecheck frontend (da `frontend/`): `npx tsc --noEmit -p tsconfig.json`

---

## Task 1: `ScrapingPool.next` — round-robin con skip dei capped

**Files:**
- Create: `backend/app/services/scraping_pool.py`
- Test: `backend/tests/test_scraping_pool.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_scraping_pool.py
"""Unit + integration tests per il round-robin multi-account scraping (Approccio C)."""
from types import SimpleNamespace

import pytest

from app.services.scraping_pool import ScrapingPool


def _entry(account_id, lookups, client):
    acct = SimpleNamespace(id=account_id, username=account_id, scrape_lookups_today=lookups)
    return {"account": acct, "client": client, "slot_owned": True}


def _campaign(limit=180):
    # has_scrape_budget legge scrape_daily_limit_for(account, campaign):
    # campaign.scrape_daily_limit override, altrimenti settings globale.
    return SimpleNamespace(scrape_daily_limit=limit)


class TestScrapingPoolNext:
    def test_alternates_between_two_accounts(self):
        pool = ScrapingPool([_entry("A", 0, "clientA"), _entry("B", 0, "clientB")])
        camp = _campaign()
        seq = [pool.next(camp)[0].id for _ in range(4)]
        assert seq == ["A", "B", "A", "B"]

    def test_single_account_always_same(self):
        pool = ScrapingPool([_entry("A", 0, "clientA")])
        camp = _campaign()
        assert [pool.next(camp)[0].id for _ in range(3)] == ["A", "A", "A"]

    def test_skips_capped_account(self):
        # B è a cap (180/180) → deve restituire sempre A
        pool = ScrapingPool([_entry("A", 0, "clientA"), _entry("B", 180, "clientB")])
        camp = _campaign(limit=180)
        assert [pool.next(camp)[0].id for _ in range(3)] == ["A", "A", "A"]

    def test_returns_none_when_all_capped(self):
        pool = ScrapingPool([_entry("A", 180, "clientA"), _entry("B", 180, "clientB")])
        camp = _campaign(limit=180)
        assert pool.next(camp) is None

    def test_returns_client_with_account(self):
        pool = ScrapingPool([_entry("A", 0, "clientA")])
        acct, client = pool.next(_campaign())
        assert acct.id == "A" and client == "clientA"

    def test_empty_pool_returns_none(self):
        assert ScrapingPool([]).next(_campaign()) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_scraping_pool.py::TestScrapingPoolNext -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.scraping_pool'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/scraping_pool.py
"""Round-robin pool of pre-logged-in scraping accounts for a campaign.

Approccio C: tutti gli account scraping/both assegnati alla campagna vengono
loggati una volta e tenuti in memoria; il bio-fetch alterna gli account per-lead
(round-robin) così il carico è condiviso dall'inizio, ognuno sul proprio IP/proxy.
Job singolo seriale — nessun worker parallelo.
"""
import json
from datetime import datetime

from loguru import logger

from app.services.account_manager import has_scrape_budget


class ScrapingPoolEmpty(Exception):
    """Nessun account utilizzabile nel pool (tutti a cap o nessuno loggato)."""


class ScrapingPool:
    def __init__(self, entries: list[dict]):
        # entries: list[{"account": InstagramAccount, "client": Client, "slot_owned": bool}]
        self._entries = list(entries)
        self._idx = 0

    @property
    def size(self) -> int:
        return len(self._entries)

    def all_accounts(self) -> list:
        return [e["account"] for e in self._entries]

    def next(self, campaign):
        """Round-robin: ritorna (account, client) con budget residuo, o None se tutti a cap/vuoto."""
        n = len(self._entries)
        if n == 0:
            return None
        for _ in range(n):
            entry = self._entries[self._idx % n]
            self._idx = (self._idx + 1) % n
            acct = entry["account"]
            if has_scrape_budget(acct, campaign):
                return acct, entry["client"]
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_scraping_pool.py::TestScrapingPoolNext -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/scraping_pool.py backend/tests/test_scraping_pool.py
git commit -m "feat(scraper): ScrapingPool round-robin next() con skip dei capped"
```

---

## Task 2: `_eligible_scraping_accounts` helper + `ScrapingPool.build/release/save_sessions`

**Files:**
- Modify: `backend/app/services/scraper.py` (aggiungi helper dopo `_get_fallback_account`, ~riga 278)
- Modify: `backend/app/services/scraping_pool.py`

- [ ] **Step 1: Aggiungi l'helper di query in `scraper.py`**

Aggiungi questa funzione subito dopo `_get_fallback_account` (dopo la riga 277):

```python
async def _eligible_scraping_accounts(db, campaign_id: str) -> list[InstagramAccount]:
    """Tutti gli account attivi con ruolo scraping/both assegnati alla campagna."""
    from sqlalchemy import select
    from app.models.campaign_account import CampaignAccount

    eligible_sq = select(CampaignAccount.account_id).where(
        CampaignAccount.campaign_id == campaign_id,
        CampaignAccount.is_active == True,
        CampaignAccount.role.in_(("scraping", "both")),
    )
    result = await db.execute(
        select(InstagramAccount).where(
            InstagramAccount.status == AccountStatus.active,
            InstagramAccount.id.in_(eligible_sq),
        )
    )
    return list(result.scalars().all())
```

- [ ] **Step 2: Aggiungi `build`, `release`, `save_sessions` a `ScrapingPool`**

Aggiungi questi metodi nella classe `ScrapingPool` (dopo `next`):

```python
    @classmethod
    async def build(cls, db, campaign) -> "ScrapingPool":
        """Pre-logga tutti gli account scraping/both della campagna nel pool."""
        from app.services.scraper import _eligible_scraping_accounts
        from app.utils.instagrapi_client import (
            acquire_scraping_slot, release_scraping_slot, login as _login,
        )

        accounts = await _eligible_scraping_accounts(db, campaign.id)
        if not accounts:
            raise ScrapingPoolEmpty(
                "Nessun account con ruolo 'scraping' o 'both' assegnato a questa campagna."
            )

        entries: list[dict] = []
        for acct in accounts:
            slot_owned = await acquire_scraping_slot(acct.id)
            if not slot_owned:
                logger.warning(
                    f"[ScrapingPool] Slot @{acct.username} già occupato da un'altra campagna — escluso dal pool"
                )
                continue
            try:
                client = await _login(acct, db)
            except Exception as e:
                await release_scraping_slot(acct.id)
                logger.warning(f"[ScrapingPool] Login fallito per @{acct.username}: {e} — escluso dal pool")
                continue
            entries.append({"account": acct, "client": client, "slot_owned": True})

        if not entries:
            raise ScrapingPoolEmpty(
                "Nessun account scraping disponibile/loggato per la campagna (slot occupati o login falliti)."
            )
        logger.info(f"[ScrapingPool] Pool costruito con {len(entries)} account: "
                    f"{', '.join('@' + e['account'].username for e in entries)}")
        return cls(entries)

    async def release(self) -> None:
        """Rilascia gli slot di tutti gli account del pool."""
        from app.utils.instagrapi_client import release_scraping_slot
        for e in self._entries:
            if e["slot_owned"]:
                await release_scraping_slot(e["account"].id)

    async def save_sessions(self, db) -> None:
        """Salva session_data + last_activity per ogni account del pool (evita re-login)."""
        for e in self._entries:
            try:
                e["account"].session_data = json.dumps(e["client"].get_settings())
                e["account"].last_activity_at = datetime.utcnow()
            except Exception as exc:
                logger.warning(f"[ScrapingPool] save session @{e['account'].username} fallito: {exc}")
        await db.commit()
```

- [ ] **Step 3: Run unit test (nessuna regressione su `next`)**

Run: `python -m pytest tests/test_scraping_pool.py::TestScrapingPoolNext -v`
Expected: PASS (6 passed)

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/scraper.py backend/app/services/scraping_pool.py
git commit -m "feat(scraper): ScrapingPool.build/release/save_sessions + _eligible_scraping_accounts"
```

---

## Task 3: Rewire `_store_followers_batch` al round-robin per-lead

**Files:**
- Modify: `backend/app/services/scraper.py:495-684` (sostituisci l'intera funzione `_store_followers_batch`)
- Test: `backend/tests/test_scraping_pool.py`

- [ ] **Step 1: Write the failing integration test**

Aggiungi in `backend/tests/test_scraping_pool.py`:

```python
class TestStoreFollowersRoundRobin:
    """_store_followers_batch deve alternare gli account del pool per-lead."""

    @pytest.mark.asyncio
    async def test_user_info_alternates_accounts(self, monkeypatch):
        import app.services.scraper as scraper
        from unittest.mock import AsyncMock, MagicMock

        # follower shorts da scrapare (pk diversi)
        shorts = []
        for i in range(4):
            s = SimpleNamespace(
                pk=str(1000 + i), username=f"u{i}", full_name=f"U{i}",
                is_private=False, profile_pic_url=None,
            )
            shorts.append(s)

        # due client mock con user_info_v1 che ritorna un oggetto bio minimale
        def make_client(tag):
            c = MagicMock(name=f"client-{tag}")
            info = SimpleNamespace(
                biography="bio", is_verified=False, follower_count=1,
                following_count=1, external_url=None,
            )
            c.user_info_v1 = MagicMock(return_value=info)
            return c
        clientA, clientB = make_client("A"), make_client("B")
        pool = ScrapingPool([_entry("A", 0, clientA), _entry("B", 0, clientB)])

        # campaign + db fake
        camp = SimpleNamespace(
            id="camp1", scrape_daily_limit=180, bio_fetch_delay_min=0, bio_fetch_delay_max=0,
            status=scraper.CampaignStatus.scraping,
        )
        db = MagicMock()
        db.refresh = AsyncMock()
        db.commit = AsyncMock()
        db.add = MagicMock()
        # nessun duplicato in DB
        exec_res = MagicMock()
        exec_res.scalar_one_or_none = MagicMock(return_value=None)
        db.execute = AsyncMock(return_value=exec_res)

        # stub delle dipendenze esterne
        monkeypatch.setattr(scraper, "is_halted", AsyncMock(return_value=False))
        monkeypatch.setattr(scraper, "increment_scrape_lookup", AsyncMock())
        monkeypatch.setattr(scraper, "extract_contacts", lambda info: scraper.ContactData())
        monkeypatch.setattr(scraper, "upsert_lead", AsyncMock())

        stored = await scraper._store_followers_batch(shorts, camp, db, pool, "followers")

        assert stored == 4
        # 4 lead, alternati A,B,A,B → 2 call per client
        assert clientA.user_info_v1.call_count == 2
        assert clientB.user_info_v1.call_count == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_scraping_pool.py::TestStoreFollowersRoundRobin -v`
Expected: FAIL — la firma attuale di `_store_followers_batch` non accetta `pool` (TypeError sul parametro), o usa `client`/`account` non passati.

- [ ] **Step 3: Sostituisci l'intera funzione `_store_followers_batch`**

Sostituisci da `async def _store_followers_batch(` (riga 495) fino al suo `return` finale (riga 684) con:

```python
async def _store_followers_batch(
    followers_batch, campaign: Campaign, db, pool, scrape_mode: str = 'followers',
) -> int:
    """
    Store a batch of followers/following in DB, fetching detailed bio for each.

    Approccio C: ogni lead usa il prossimo account del pool (round-robin). Il cap
    per-account è gestito da pool.next (salta gli account a cap; None = tutti a cap).
    Su 429/soft-block si ruota al prossimo account del pool e si riprova una volta.

    Returns stored_count.
    """
    from sqlalchemy import select

    stored = 0
    consecutive_soft_blocks = 0

    for user_short in followers_batch:
        if await is_halted(db):
            logger.warning(f"[Scraper] Global BOT_HALTED mid-batch - stopping after {stored} profiles")
            raise BotHaltedError("global kill-switch active")

        # Check campaign status before each profile — lets pause/stop take effect quickly.
        await db.refresh(campaign)
        _SCRAPING_STATES = (CampaignStatus.scraping, CampaignStatus.scraping_and_running)
        if campaign.status not in _SCRAPING_STATES:
            logger.info(
                f"[Scraper] Campaign status='{campaign.status.value}' detected mid-batch "
                f"after {stored} profiles — stopping immediately."
            )
            return stored

        # Check for duplicate
        existing = await db.execute(
            select(Follower).where(
                Follower.campaign_id == campaign.id,
                Follower.ig_user_id == int(user_short.pk),
            )
        )
        if existing.scalar_one_or_none():
            continue

        # Round-robin: prossimo account con budget. None = tutti a cap.
        sel = pool.next(campaign)
        if sel is None:
            raise ScrapeBudgetError(
                "Cap lookup giornaliero raggiunto su tutti gli account scraping disponibili"
            )
        current_account, current_client = sel

        biography = None
        is_verified = False
        follower_count = None
        following_count = None
        external_url = None
        contacts = ContactData()

        for attempt in range(2):
            try:
                # user_info_v1 usa solo l'API privata autenticata (/api/v1/users/{pk}/info/).
                user_info = await asyncio.to_thread(current_client.user_info_v1, user_short.pk)
                biography = user_info.biography or None
                is_verified = getattr(user_info, 'is_verified', False) or False
                follower_count = getattr(user_info, 'follower_count', None)
                following_count = getattr(user_info, 'following_count', None)
                ext = getattr(user_info, 'external_url', None)
                external_url = str(ext) if ext else None
                contacts = extract_contacts(user_info)
                await increment_scrape_lookup(db, current_account.id)
                current_account.scrape_lookups_today = (current_account.scrape_lookups_today or 0) + 1
                consecutive_soft_blocks = 0
                break
            except Exception as e:
                error_str = str(e).lower()
                is_rate_limit = "429" in error_str or "too many" in error_str or "rate" in error_str
                is_soft_block = "protect" in error_str or "restrict" in error_str or "community" in error_str
                if (is_rate_limit or is_soft_block) and attempt == 0:
                    kind = "Soft block" if is_soft_block else "429"
                    alt = pool.next(campaign)
                    if alt is not None and alt[0].id != current_account.id:
                        logger.warning(
                            f"[Scraper] {kind} su user_info @{user_short.username}. "
                            f"Rotazione pool: @{current_account.username} → @{alt[0].username}"
                        )
                        current_account, current_client = alt
                        await asyncio.sleep(random.uniform(30 if is_soft_block else 15, 60 if is_soft_block else 30))
                    else:
                        wait = random.uniform(120, 240) if is_soft_block else 60
                        logger.warning(
                            f"[Scraper] {kind} su user_info @{user_short.username}, "
                            f"nessun account alternativo nel pool. Attendo {int(wait)}s..."
                        )
                        await asyncio.sleep(wait)
                else:
                    if is_rate_limit or is_soft_block:
                        consecutive_soft_blocks += 1
                        kind = "Soft block" if is_soft_block else "429"
                        logger.warning(
                            f"[Scraper] {kind} persistente su @{user_short.username} dopo retry. "
                            f"Profilo salvato senza bio ({consecutive_soft_blocks} consecutivi)."
                        )
                        await asyncio.sleep(random.uniform(90 if is_soft_block else 30, 180 if is_soft_block else 60))
                    else:
                        logger.warning(f"Could not fetch bio for @{user_short.username}: {e}")
                    break

        if consecutive_soft_blocks >= 3:
            raise SoftBlockError(
                f"3 soft block consecutivi — Instagram blocca attivamente la bio fetch. "
                f"Interruzione per proteggere @{current_account.username}."
            )

        follower = Follower(
            campaign_id=campaign.id,
            ig_user_id=int(user_short.pk),
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
        await db.commit()
        stored += 1

        await upsert_lead(
            db,
            ig_user_id=int(user_short.pk),
            username=user_short.username,
            full_name=user_short.full_name,
            biography=biography,
            contacts=contacts,
            campaign=campaign,
            account=current_account,
        )

        # Delay configurabile tra bio fetch (per-campagna). NB: è GLOBALE per-lead,
        # condiviso tra gli account del pool (vedi helper text UI).
        delay_min = getattr(campaign, 'bio_fetch_delay_min', 5.0) or 5.0
        delay_max = getattr(campaign, 'bio_fetch_delay_max', 8.0) or 8.0
        delay = random.uniform(delay_min, delay_max)
        await asyncio.sleep(delay)

    return stored
```

> NOTA: rimuovi la chiamata a `upsert_lead` solo se è duplicata; il blocco sopra la include già una volta — non aggiungerne altre. Verifica che dopo la sostituzione non resti codice orfano (vecchie righe 642-684).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_scraping_pool.py::TestStoreFollowersRoundRobin -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/scraper.py backend/tests/test_scraping_pool.py
git commit -m "feat(scraper): round-robin per-lead in _store_followers_batch via ScrapingPool"
```

---

## Task 4: Rewire `_scrape_paginated` per usare il pool

**Files:**
- Modify: `backend/app/services/scraper.py:305-385` (firma + corpo di `_scrape_paginated`)

- [ ] **Step 1: Cambia la firma e l'uso del client di paginazione**

Sostituisci la definizione (riga 305):

```python
async def _scrape_paginated(client, campaign: Campaign, account: InstagramAccount, db, scrape_mode: str = 'followers') -> tuple[int, str]:
```

con:

```python
async def _scrape_paginated(pool, campaign: Campaign, db, scrape_mode: str = 'followers') -> tuple[int, str]:
```

Subito dopo la docstring (prima di `mode_label = ...`), aggiungi la scelta del client di paginazione:

```python
    # La paginazione lista resta su UN account del pool (chiamate cheap, non vanno ruotate).
    list_sel = pool.next(campaign)
    if list_sel is None:
        raise ScrapeBudgetError("Cap raggiunto su tutti gli account scraping (paginazione)")
    list_account, list_client = list_sel
```

- [ ] **Step 2: Aggiorna le chiamate interne**

Nel corpo di `_scrape_paginated`:

1. La fetch del chunk (riga ~345) usa `list_client`:

```python
            followers_batch, max_id = await asyncio.to_thread(
                _fetch_followers_chunk, list_client, campaign.target_user_id, batch_size, max_id, scrape_mode
            )
```

2. La chiamata a `_store_followers_batch` (riga ~367) diventa:

```python
            batch_total = await _store_followers_batch(
                followers_batch, campaign, db, pool, scrape_mode,
            )
```

3. Sostituisci il salvataggio sessione single-account (righe ~379-382):

```python
            # Save session for whichever account is currently active
            account.session_data = json.dumps(client.get_settings())
            account.last_activity_at = datetime.utcnow()
            await db.commit()
```

con il salvataggio di tutte le sessioni del pool:

```python
            # Salva le sessioni di tutti gli account del pool (commit incluso)
            campaign.total_followers = initial_total + total
            campaign.scrape_cursor = max_id
            campaign.updated_at = datetime.utcnow()
            await pool.save_sessions(db)
```

> NB: rimuovi le righe duplicate di `campaign.total_followers/scrape_cursor/updated_at` che precedevano (righe ~374-382), perché ora le impostiamo subito prima di `pool.save_sessions`. Assicurati che restino impostate una volta sola per batch.

- [ ] **Step 3: Run i test esistenti del pool (no regressione import)**

Run: `python -m pytest tests/test_scraping_pool.py -v`
Expected: PASS (tutti)

- [ ] **Step 4: Verifica che il modulo importi senza errori**

Run: `python -c "import app.services.scraper"`
Expected: nessun output, nessuna eccezione (verifica che non siano rimasti riferimenti a `client`/`account` non definiti in `_scrape_paginated`).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/scraper.py
git commit -m "refactor(scraper): _scrape_paginated usa ScrapingPool (lista su 1 account, bio round-robin)"
```

---

## Task 5: Rewire l'entry `scrape_followers` al pool

**Files:**
- Modify: `backend/app/services/scraper.py:80-123` (setup account) e `:219-221` (finally)

- [ ] **Step 1: Sostituisci il setup single-account con il pool**

Sostituisci il blocco (righe 80-88):

```python
        _scraping_account_id = None
        try:
            account = await _get_available_account(db, campaign_id=campaign_id)
            if await acquire_scraping_slot(account.id):
                _scraping_account_id = account.id
            else:
                logger.warning(f"[Scraper] Slot @{account.username} già occupato (TOCTOU) — condiviso")
            client = await _login(account, db)
            emit_event(campaign_id, "scrape_start", f"Account @{account.username} connesso, inizio raccolta {mode_label}...")
```

con:

```python
        pool = None
        try:
            pool = await ScrapingPool.build(db, campaign)
            sel = pool.next(campaign)
            if sel is None:
                raise ScrapeBudgetError("Cap raggiunto su tutti gli account scraping all'avvio")
            account, client = sel  # usato per la risoluzione target
            emit_event(
                campaign_id, "scrape_start",
                f"{pool.size} account scraping connessi, inizio raccolta {mode_label}...",
            )
```

- [ ] **Step 2: Aggiorna la chiamata a `_scrape_paginated`**

Sostituisci (riga 123):

```python
            total_scraped, scrape_outcome = await _scrape_paginated(client, campaign, account, db, scrape_mode)
```

con:

```python
            total_scraped, scrape_outcome = await _scrape_paginated(pool, campaign, db, scrape_mode)
```

- [ ] **Step 3: Aggiorna il `finally`**

Sostituisci (righe 219-221):

```python
        finally:
            if _scraping_account_id:
                await release_scraping_slot(_scraping_account_id)
```

con:

```python
        finally:
            if pool is not None:
                try:
                    await pool.save_sessions(db)
                except Exception as exc:
                    logger.warning(f"[Scraper] save_sessions finale fallito: {exc}")
                await pool.release()
```

- [ ] **Step 4: Aggiungi l'import di `ScrapingPool` in cima a `scraper.py`**

Dopo gli import esistenti (dopo riga 41, `from app.utils.exceptions import ScrapeBudgetError`):

```python
from app.services.scraping_pool import ScrapingPool, ScrapingPoolEmpty
```

E gestisci `ScrapingPoolEmpty` come errore campagna: nel blocco `except Exception as e:` (riga 214) è già coperto (diventa `status=error`). Opzionale: aggiungi un `except ScrapingPoolEmpty` dedicato prima di `except Exception` per emettere un messaggio chiaro:

```python
        except ScrapingPoolEmpty as e:
            logger.error(f"Scrape non avviato: {e}")
            campaign.status = CampaignStatus.error
            await db.commit()
            emit_event(campaign_id, "scrape_stopped", f"Scraping non avviato: {e}", level="error")
```

- [ ] **Step 5: Verifica import + run pool tests**

Run: `python -c "import app.services.scraper"`
Expected: nessuna eccezione.

Run: `python -m pytest tests/test_scraping_pool.py -v`
Expected: PASS (tutti).

- [ ] **Step 6: Run l'intera suite backend (no regressioni)**

Run: `python -m pytest -q`
Expected: nessun nuovo fallimento rispetto a prima del lavoro (annota eventuali test pre-esistenti già rossi).

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/scraper.py
git commit -m "feat(scraper): scrape_followers costruisce e rilascia ScrapingPool (multi-account round-robin)"
```

---

## Task 6: Helper text delay — form nuova campagna

**Files:**
- Modify: `frontend/app/campaigns/new/page.tsx:399` (dopo la chiusura del grid dei campi advanced)

- [ ] **Step 1: Inserisci la nota esplicativa**

Dopo il `</div>` che chiude la `grid` dei campi avanzati (riga 399, subito prima del `</div>` che chiude la sezione), inserisci:

```tsx
                  <p className="col-span-2 text-xs text-amber-500/90 bg-amber-950/30 border border-amber-800/40 rounded px-2 py-1.5 mt-1">
                    ⚠️ <strong>Questi tempi valgono per OGNI lead estratto, condivisi tra tutti gli account scraping.</strong>{' '}
                    Con più account il delay si applica tra un account e il successivo: ogni singolo account aspetta circa
                    (n° account × questo valore) tra i suoi lead. Esempio: 2 account e vuoi ~6–10s per account → imposta <strong>3–5s</strong>.
                  </p>
```

> Verifica che il `<p>` finisca DENTRO il container `grid grid-cols-2` (così `col-span-2` ha effetto) oppure, se lo metti fuori dal grid, rimuovi `col-span-2`. Apri il file e posiziona coerentemente con la struttura JSX reale.

- [ ] **Step 2: Typecheck**

Run (da `frontend/`): `npx tsc --noEmit -p tsconfig.json`
Expected: nessun errore.

- [ ] **Step 3: Commit**

```bash
git add frontend/app/campaigns/new/page.tsx
git commit -m "feat(ui): helper text delay per-lead nel form nuova campagna"
```

---

## Task 7: Helper text delay — modale impostazioni (ingranaggio)

**Files:**
- Modify: `frontend/app/campaigns/[id]/page.tsx:1699` (dentro la sezione Scraping del modale, dopo il grid dei delay)

- [ ] **Step 1: Inserisci la nota**

Subito dopo il `</div>` che chiude `grid grid-cols-2 gap-3` della sezione Scraping (riga 1699), prima del `</div>` che chiude la sezione (riga 1700), inserisci:

```tsx
                  <p className="text-xs text-amber-500/90 bg-amber-950/30 border border-amber-800/40 rounded px-2 py-1.5">
                    ⚠️ <strong>I delay valgono per OGNI lead, condivisi tra tutti gli account scraping.</strong>{' '}
                    Con N account ogni account aspetta circa N× questo valore tra i suoi lead. Con 2 account, per ~6–10s
                    effettivi per account imposta <strong>3–5s</strong>.
                  </p>
```

- [ ] **Step 2: Typecheck**

Run (da `frontend/`): `npx tsc --noEmit -p tsconfig.json`
Expected: nessun errore.

- [ ] **Step 3: Commit**

```bash
git add frontend/app/campaigns/[id]/page.tsx
git commit -m "feat(ui): helper text delay per-lead nel modale impostazioni campagna"
```

---

## Task 8: Verifica finale + documentazione

**Files:**
- Modify: `CLAUDE.md` (sezione "Scraping avanzato"), `INDEX.md`, `docs/project/PROGRESS.md`, memory `project_state.md`

- [ ] **Step 1: Suite backend completa**

Run (da `backend/`): `python -m pytest -q`
Expected: nessun nuovo fallimento.

- [ ] **Step 2: Typecheck frontend completo**

Run (da `frontend/`): `npx tsc --noEmit -p tsconfig.json`
Expected: nessun errore.

- [ ] **Step 3: Verifica manuale comportamento (checklist, no invio reale)**

Con 2 account ruolo `scraping`/`both` assegnati a una campagna `draft`:
- Avvia scraping. Nei log deve comparire `[ScrapingPool] Pool costruito con 2 account: @a, @b`.
- Osserva i log `user_info_v1`: gli account devono alternarsi (A, B, A, B…).
- Verifica su DB che `scrape_lookups_today` cresca ~equamente su entrambi gli account (es. dopo 20 lead ≈ 10/10).
- Manda una campagna in session break: il box "Pausa sessione" + countdown header devono ancora comparire (break campagna-level invariato).
- Con 1 solo account assegnato: comportamento identico a prima (nessuna regressione).

- [ ] **Step 4: Aggiorna documentazione**

In `CLAUDE.md`, sezione "Scraping avanzato + raccolta contatti", aggiungi una riga:

```markdown
- **Multi-account round-robin scraping**: con 2+ account `scraping`/`both` su una campagna, il bio-fetch alterna gli account per-lead (`ScrapingPool` in `app/services/scraping_pool.py`), condividendo il carico dall'inizio (prima era sequenziale: A fino al cap, poi B). Job singolo seriale, break campagna-level invariato. ⚠️ Il delay `bio_fetch_delay` è GLOBALE per-lead: con N account ogni account attende ~N× il valore (UI lo segnala).
```

Aggiorna `INDEX.md` (tabella file critici: aggiungi `scraping_pool.py`) e `docs/project/PROGRESS.md` (nuova voce datata). Aggiorna la memory `project_state.md` segnando lo spec come IMPLEMENTATO.

- [ ] **Step 5: Commit finale**

```bash
git add CLAUDE.md INDEX.md docs/project/PROGRESS.md docs/superpowers/
git commit -m "docs: multi-account round-robin scraping (Approccio C) implementato"
```

---

## Self-Review (compilato)

- **Spec coverage**: pool pre-login (T2/T5) ✓; round-robin per-lead (T3) ✓; cap per-account via pool.next (T1/T3) ✓; 429/soft-block rotazione pool (T3) ✓; paginazione su 1 account (T4) ✓; break campagna-level invariato (nessuna modifica al break = preservato) ✓; fine/release slot multipli (T5) ✓; save_sessions multiple (T2/T4/T5) ✓; compat mono-account (test T1 `test_single_account_always_same` + checklist T8) ✓; helper text form+modale (T6/T7) ✓; import fuori scope (non toccato) ✓.
- **Placeholder scan**: nessun TBD/TODO; codice completo in ogni step.
- **Type consistency**: `pool.next(campaign)` ritorna `(account, client)` o `None` ovunque; `_store_followers_batch(...)->int`; `_scrape_paginated(pool, campaign, db, scrape_mode)->tuple[int,str]`; firme coerenti tra T3/T4/T5.
- **Rischio residuo**: la sostituzione di funzioni lunghe in `scraper.py` richiede attenzione a non lasciare codice orfano — gli step T3/T4 lo segnalano esplicitamente; lo `python -c "import app.services.scraper"` (T4 S4, T5 S5) intercetta riferimenti rotti.
