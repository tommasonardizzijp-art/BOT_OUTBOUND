# Anti-detection Scraping — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rendere lo scraping (Fase Bio) meno rilevabile da Instagram, alzando la fedeltà "app-like" delle chiamate API e distribuendo attività browser organica su tutti gli account.

**Architecture:** Tre interventi indipendenti sulla Fase Bio, tutti dietro flag/config, applicati a bot fermo con un solo restart finale. (A) chiamata profilo app-like invece del bare `user_info_v1`; (B) cap sessione randomico persistito; (C) attività browser (scroll + batch scraping) nella pausa su TUTTI gli account, scaglionati. Il delay lognormale tra lookup (punto 2) è già fatto e in `main` del branch.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy async + Alembic, instagrapi 2.3.0 (private API), Patchright (browser), ARQ (task queue), pytest.

## Global Constraints

- **Bot FERMO durante l'applicazione.** Un solo restart worker alla fine, con `warmup_browse_enabled=True` + `bio_browser_batch_enabled=True`.
- **Tutto dietro flag/config; default OFF** dove introduce comportamento nuovo. Zero impatto finché non si accende.
- **Difensivo**: nessuna aggiunta anti-detection deve poter far fallire lo scraping. Errori browser/estrazione ingoiati e loggati.
- **instagrapi 2.3.0, NESSUN upgrade** (rimandato). Emula app IG 364.0.0.35.86 / 385.0.0.47.74.
- **Anti-divergenza**: bio via browser e via API scrivono gli STESSI campi Follower + `upsert_lead` passando per lo stesso `extract_contacts`.
- **Coerenza IP**: browser e API mobile escono dallo STESSO `account.proxy` (per costruzione: entrambi leggono `account.proxy`).
- **DB PROD Supabase** (`gnsucw…`), NON il DB test/Primero. Migrazioni via `python -m scripts.migrate` a bot fermo.
- Ogni modifica al pacing/fedeltà con un **test che ne blocca la regressione**.
- Console Windows cp1252: log **ASCII-only**.

---

## File Structure

- `app/services/scraper.py` — MODIFY `fetch_and_store_bio` / il punto della chiamata `user_info_v1`: nuova chiamata profilo app-like (Task 1-3).
- `app/services/profile_lookup.py` — CREATE: helper puro che costruisce params app-like e decide l'endpoint (Task 1, 3). Isola la logica "come chiamare il profilo" dallo storage.
- `scripts/spike_full_detail_info.py` — CREATE: spike di validazione live (Task 2). Non è codice di produzione.
- `app/models/campaign.py` — MODIFY: colonna `current_session_cap` (Task 4).
- `alembic/versions/019_current_session_cap.py` — CREATE: migrazione colonna (Task 4).
- `app/services/scrape_bios.py` — MODIFY: cap randomico persistito al posto del 250 fisso (Task 4); chiamata all'orchestratore multi-account in pausa (Task 5).
- `app/services/browser_bio.py` — MODIFY: `run_pause_browser_activity` apre la propria sessione DB (parallel-safe); nuovo `run_pause_browser_all_accounts` (Task 5).
- `app/config.py` — MODIFY: `bio_session_cap_min/max`, `bio_lookup_from_module` (Task 1, 4).
- `tests/` — vari test nuovi.

**Out of scope (follow-up separati):** upgrade instagrapi (punto 5), Fase Lista (punto 7), validazione live del rate `web_profile_info` browser (punto 6 — sessione di test dedicata, non blocca questo piano).

---

### Task 1: Params profilo app-like (`from_module`) — fix a basso rischio

Il bare `user_info_v1(pk)` usa `from_module="self_profile"` di default → ogni lookup su profili altrui dichiara "sto guardando il mio profilo". Un'apertura profilo reale (da feed/ricerca) usa `from_module="feed_timeline"` → `entry_point="profile"`. instagrapi supporta il param (assert su `INFO_FROM_MODULES` = self_profile | feed_timeline | reel_feed_timeline). Questo fix non aggiunge chiamate, cambia solo la semantica dichiarata.

**Files:**
- Create: `app/services/profile_lookup.py`
- Test: `tests/test_profile_lookup_params.py`

**Interfaces:**
- Produces: `pick_from_module() -> str` (ritorna un modulo realistico non-self, variato), consumato in Task 3.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_profile_lookup_params.py
from app.services.profile_lookup import pick_from_module

def test_from_module_is_never_self_profile():
    # Su migliaia di lookup di ALTRI profili, "self_profile" non deve mai comparire.
    vals = {pick_from_module() for _ in range(500)}
    assert "self_profile" not in vals

def test_from_module_only_valid_instagrapi_values():
    # instagrapi assert-a from_module in INFO_FROM_MODULES.
    allowed = {"feed_timeline", "reel_feed_timeline"}
    vals = {pick_from_module() for _ in range(500)}
    assert vals.issubset(allowed)

def test_from_module_varies():
    vals = {pick_from_module() for _ in range(500)}
    assert len(vals) >= 2  # non un singolo valore fisso
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/Scripts/python.exe -m pytest tests/test_profile_lookup_params.py -q`
Expected: FAIL (ModuleNotFoundError: app.services.profile_lookup)

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/profile_lookup.py
"""Come chiamare il profilo in modo app-like. Puro, testabile, niente IO."""
import random

# Moduli validi per instagrapi user_info_v1 che NON sono "self_profile"
# (self_profile = stai guardando il TUO profilo; per profili altrui = firma bot).
# Entrambi settano entry_point="profile" lato instagrapi.
_REALISTIC_MODULES = ("feed_timeline", "reel_feed_timeline")
# Pesato: la maggior parte delle aperture profilo arriva dal feed principale.
_WEIGHTS = (0.85, 0.15)

def pick_from_module() -> str:
    """Ritorna un from_module realistico per l'apertura di un profilo ALTRUI."""
    return random.choices(_REALISTIC_MODULES, weights=_WEIGHTS, k=1)[0]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/Scripts/python.exe -m pytest tests/test_profile_lookup_params.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/profile_lookup.py backend/tests/test_profile_lookup_params.py
git commit -m "feat(bio): from_module app-like per lookup profilo (no self_profile)"
```

---

### Task 2: Spike di validazione `full_detail_info` (GATE del punto 4b)

Prima di impegnarci su `full_detail_info` va confermato LIVE che: (a) l'endpoint risponde 200 sulla sessione mobile reale; (b) la risposta contiene i campi contatto che ci servono (`user` con biography/public_email/public_phone_number/bio_links). Se NON è pulito, il punto 4b ripiega su `user_medias_v1` (Task 3, ramo B). **Questo task non produce codice di produzione: è un accertamento.**

**Files:**
- Create: `scripts/spike_full_detail_info.py`

- [ ] **Step 1: Scrivi lo script di spike**

```python
# backend/scripts/spike_full_detail_info.py
"""Spike: verifica full_detail_info su un account reale. NON codice di produzione.
Uso: ./venv/Scripts/python.exe -m scripts.spike_full_detail_info <account_id> <target_pk>
Stampa lo status, le chiavi top-level, e se i campi contatto sono presenti sotto user."""
import asyncio, sys, json
from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount
from sqlalchemy import select
from app.utils.instagrapi_client import login

async def main(account_id: str, target_pk: str):
    async with AsyncSessionLocal() as db:
        acc = (await db.execute(select(InstagramAccount).where(InstagramAccount.id == account_id))).scalar_one()
        client = await login(acc, db)
    def _call():
        return client.private_request(f"users/{target_pk}/full_detail_info/")
    result = await asyncio.to_thread(_call)
    print("TOP-LEVEL KEYS:", list(result.keys()))
    user = (result.get("user_detail") or {}).get("user") or result.get("user") or {}
    print("HAS biography:", "biography" in user)
    print("HAS public_email:", user.get("public_email"))
    print("HAS public_phone_number:", user.get("public_phone_number"))
    print("HAS feed/media items:", bool(result.get("feed") or result.get("reels_media")))
    # Dump completo su file per ispezione.
    with open("scratch_full_detail_info.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print("Dump completo -> scratch_full_detail_info.json")

if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
```

- [ ] **Step 2: Esegui lo spike (Tommaso, con un account loggato + un pk target reale)**

Run: `./venv/Scripts/python.exe -m scripts.spike_full_detail_info <account_id> <target_pk>`
Expected: stampa `HAS biography: True` e un dump JSON. **DECISIONE:**
- Se 200 + `user` con i campi contatto → **ramo A** (Task 3 usa `full_detail_info`).
- Se errore/manca `user`/campi assenti → **ramo B** (Task 3 usa `user_info_v1` app-like + `user_medias_v1`).

- [ ] **Step 3: Annota l'esito nel piano**

Scrivi in cima al Task 3 quale ramo (A o B) è stato scelto e perché. Nessun commit di codice (lo script spike può essere committato come utility o scartato).

---

### Task 3: Chiamata profilo app-like — implementazione (punto 4b)

Sostituisce il bare `user_info_v1` nella Fase Bio con una chiamata che assomiglia all'apertura profilo dell'app. **Ramo scelto in Task 2.** Entrambi i rami passano per lo STESSO `extract_contacts` + storage (anti-divergenza). Fallback a `user_info_v1` semplice su errore, per non rompere lo scraping.

**Files:**
- Modify: `app/services/profile_lookup.py` (aggiunge la funzione di fetch)
- Modify: `app/services/scraper.py` (dentro `fetch_and_store_bio`, riga della chiamata `user_info_v1`)
- Test: `tests/test_profile_lookup_fetch.py`

**Interfaces:**
- Consumes: `pick_from_module()` (Task 1)
- Produces: `fetch_profile_app_like(client, pk) -> User` — ritorna un oggetto instagrapi `User` (stesso tipo di `user_info_v1`), così `extract_contacts` e lo storage restano invariati.

- [ ] **Step 1: Write the failing test (mapping-safe, no network)**

```python
# tests/test_profile_lookup_fetch.py
from unittest.mock import MagicMock
from app.services.profile_lookup import fetch_profile_app_like

def test_uses_app_like_from_module_not_self_profile():
    client = MagicMock()
    fake_user = object()
    client.user_info_v1.return_value = fake_user
    out = fetch_profile_app_like(client, "123")
    # Deve aver chiamato user_info_v1 con from_module != self_profile.
    _, kwargs = client.user_info_v1.call_args
    assert kwargs.get("from_module") in ("feed_timeline", "reel_feed_timeline")
    assert out is fake_user
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/Scripts/python.exe -m pytest tests/test_profile_lookup_fetch.py -q`
Expected: FAIL (ImportError: fetch_profile_app_like)

- [ ] **Step 3: Implementazione — RAMO B (default sicuro; usare A solo se lo spike lo conferma)**

```python
# app/services/profile_lookup.py  (append)
from loguru import logger

def fetch_profile_app_like(client, pk: str):
    """Recupera il profilo come farebbe l'app aprendo un contatto.

    RAMO B (default, instagrapi-supported): user_info_v1 con from_module realistico
    (non self_profile) + fetch di pochi post del profilo (feed/user), che l'app carica
    sempre all'apertura di un profilo. I post NON vengono salvati: servono solo a far
    sembrare la chiamata un'apertura profilo vera, non un bare user_info.

    Ritorna l'oggetto User (stesso tipo di user_info_v1) per lo storage invariato.
    RAMO A (se lo spike conferma full_detail_info): sostituire il corpo con
    client.private_request(f"users/{pk}/full_detail_info/") ed estrarre user_detail.user
    via extract_user_v1 (mantenere il fallback sotto).
    """
    user = client.user_info_v1(pk, from_module=pick_from_module())
    # Post-grid come l'app (best-effort, scartati). Non deve rompere la bio.
    try:
        client.user_medias_v1(pk, amount=12)  # prima pagina griglia post
    except Exception as e:
        logger.debug(f"[ProfileLookup] fetch post best-effort fallito per {pk}: {e}")
    return user
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/Scripts/python.exe -m pytest tests/test_profile_lookup_fetch.py -q`
Expected: PASS

- [ ] **Step 5: Cabla in `fetch_and_store_bio`**

In `app/services/scraper.py`, sostituire:
```python
        user_info = await asyncio.to_thread(current_client.user_info_v1, follower.ig_user_id)
```
con:
```python
        from app.services.profile_lookup import fetch_profile_app_like
        user_info = await asyncio.to_thread(fetch_profile_app_like, current_client, follower.ig_user_id)
```
(Il resto di `fetch_and_store_bio` — extract_contacts, storage, outcome — resta identico.)

- [ ] **Step 6: Run test suite bio**

Run: `./venv/Scripts/python.exe -m pytest tests/test_bio_micro_yield.py tests/test_bio_error_no_infinite_loop.py tests/test_profile_lookup_fetch.py -q`
Expected: PASS (tutti)

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/profile_lookup.py backend/app/services/scraper.py backend/tests/test_profile_lookup_fetch.py
git commit -m "feat(bio): lookup profilo app-like (from_module + post-grid) invece di bare user_info"
```

---

### Task 4: Cap sessione randomico persistito (punto 1)

Il break lungo ora scatta a 250 fisso (`scrape_session_size`). Diventa un valore random in [150, 300] per mini-sessione, **persistito in DB** perché la formula `next_long_break` deve restare deterministica ai restart del job (micro-yield). Il micro-yield (100 bio) RESTA come salvaguardia del `job_timeout=3600s` — è cosa distinta dal cap "umano"; documentarlo.

**Files:**
- Modify: `app/models/campaign.py` (colonna `current_session_cap`)
- Create: `alembic/versions/019_current_session_cap.py`
- Modify: `app/config.py` (`bio_session_cap_min=150`, `bio_session_cap_max=300`)
- Modify: `app/services/scrape_bios.py` (usa il cap persistito al posto di `size` fisso)
- Test: `tests/test_session_cap_random.py`

**Interfaces:**
- Produces: `pick_session_cap(min_v, max_v) -> int` (puro), consumato in `scrape_bios`.

- [ ] **Step 1: Write the failing test (logica pura del cap)**

```python
# tests/test_session_cap_random.py
from app.services.scrape_bios import pick_session_cap

def test_cap_in_range():
    for _ in range(500):
        c = pick_session_cap(150, 300)
        assert 150 <= c <= 300

def test_cap_varies():
    caps = {pick_session_cap(150, 300) for _ in range(500)}
    assert len(caps) > 20  # random, non fisso

def test_cap_handles_inverted():
    c = pick_session_cap(300, 150)
    assert 150 <= c <= 300
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/Scripts/python.exe -m pytest tests/test_session_cap_random.py -q`
Expected: FAIL (ImportError: pick_session_cap)

- [ ] **Step 3: Aggiungi `pick_session_cap` in `scrape_bios.py`**

```python
# app/services/scrape_bios.py  (vicino a bio_should_continue)
import random as _random

def pick_session_cap(min_v: int, max_v: int) -> int:
    """Cap random di bio per mini-sessione prima della pausa lunga. Sostituisce il
    250 fisso: un cap costante e' una firma. Va PERSISTITO (campaigns.current_session_cap)
    perche' next_long_break e' deterministico ai restart del job (micro-yield)."""
    lo, hi = (min_v, max_v) if min_v <= max_v else (max_v, min_v)
    return _random.randint(lo, hi)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/Scripts/python.exe -m pytest tests/test_session_cap_random.py -q`
Expected: PASS

- [ ] **Step 5: Colonna DB + migrazione**

In `app/models/campaign.py` aggiungere:
```python
    current_session_cap = Column(Integer, nullable=True)  # cap random della mini-sessione bio in corso
```
Creare `alembic/versions/019_current_session_cap.py`:
```python
"""current_session_cap su campaigns

Revision ID: 019_current_session_cap
Revises: 018_scrape_lookups_date
"""
from alembic import op
import sqlalchemy as sa

revision = "019_current_session_cap"
down_revision = "018_scrape_lookups_date"  # VERIFICARE l'ultima revision reale prima di applicare
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("campaigns", sa.Column("current_session_cap", sa.Integer(), nullable=True))

def downgrade():
    op.drop_column("campaigns", "current_session_cap")
```
⚠️ Verificare `down_revision` reale con `alembic heads` prima di applicare.

- [ ] **Step 6: Usa il cap persistito nel loop bio**

In `app/services/scrape_bios.py`, dove ora si calcola `size` / `next_long_break`:
```python
            size = getattr(campaign, "scrape_session_size", 250) or 250
            next_long_break = ((done // size) + 1) * size
```
sostituire con logica che fissa e riusa il cap persistito:
```python
            # Cap random per mini-sessione, persistito: fissa una volta, riusa ai restart.
            if not getattr(campaign, "current_session_cap", None):
                campaign.current_session_cap = pick_session_cap(
                    settings.bio_session_cap_min, settings.bio_session_cap_max
                )
                await db.commit()
            size = campaign.current_session_cap
            next_long_break = ((done // size) + 1) * size
```
E al momento del break lungo (dopo aver settato `scraping_break`), azzerare il cap così alla ripresa se ne pesca uno nuovo:
```python
                        campaign.current_session_cap = None  # nuova mini-sessione -> nuovo cap
```
(inserire prima del `return max(60, seconds - ...)`). Aggiungere `from app.config import settings` agli import se assente.

- [ ] **Step 7: Config**

In `app/config.py`, vicino ai settaggi scraping:
```python
    bio_session_cap_min: int = 150   # cap random mini-sessione bio (min)
    bio_session_cap_max: int = 300   # cap random mini-sessione bio (max)
```

- [ ] **Step 8: Test suite bio + commit**

Run: `./venv/Scripts/python.exe -m pytest tests/test_session_cap_random.py tests/test_bio_micro_yield.py tests/test_bio_error_no_infinite_loop.py -q`
Expected: PASS
```bash
git add backend/app/models/campaign.py backend/alembic/versions/019_current_session_cap.py backend/app/config.py backend/app/services/scrape_bios.py backend/tests/test_session_cap_random.py
git commit -m "feat(bio): cap sessione random 150-300 persistito (era 250 fisso)"
```

---

### Task 5: Attività browser in pausa su TUTTI gli account, scaglionati (punto 3)

Ora la pausa scalda solo l'ultimo account usato. Deve coprire TUTTI gli account scraping della campagna: ognuno la sua sessione scroll+batch, in **parallelo ma con partenze scaglionate** (offset random 1-3 min tra account, mai simultanei), rispettando `max_concurrent_browsers`. **Ogni task apre la PROPRIA sessione DB** (le sessioni SQLAlchemy async NON sono concorrenti-safe).

**Files:**
- Modify: `app/services/browser_bio.py` (`run_pause_browser_activity` apre la propria db session; nuovo `run_pause_browser_all_accounts`)
- Modify: `app/services/scrape_bios.py` (il break chiama l'orchestratore multi-account)
- Test: `tests/test_pause_all_accounts.py`

**Interfaces:**
- Consumes: `run_pause_browser_activity(campaign_id, account_id, username)` (rifattorizzato: prende `campaign_id`, apre la sua db session)
- Produces: `run_pause_browser_all_accounts(campaign_id) -> int` (secondi totali spesi), consumato da `scrape_bios`.

- [ ] **Step 1: Write the failing test (orchestrazione: N account, staggered, semaphore)**

```python
# tests/test_pause_all_accounts.py
import asyncio
import app.services.browser_bio as bb

def test_all_accounts_get_a_session(monkeypatch):
    called = []
    async def fake_activity(campaign_id, account_id, username=None):
        called.append(account_id)
        return 1
    async def fake_accounts(campaign_id):
        return [("a1", "u1"), ("a2", "u2"), ("a3", "u3")]
    # niente sleep reale
    async def no_sleep(_): return None
    monkeypatch.setattr(bb, "run_pause_browser_activity", fake_activity)
    monkeypatch.setattr(bb, "_scraping_accounts_of_campaign", fake_accounts)
    monkeypatch.setattr(bb.asyncio, "sleep", no_sleep)
    asyncio.run(bb.run_pause_browser_all_accounts("camp1"))
    assert set(called) == {"a1", "a2", "a3"}  # tutti coperti
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/Scripts/python.exe -m pytest tests/test_pause_all_accounts.py -q`
Expected: FAIL (AttributeError: run_pause_browser_all_accounts / _scraping_accounts_of_campaign)

- [ ] **Step 3: Rifattorizza `run_pause_browser_activity` per aprire la sua db session**

In `app/services/browser_bio.py`, cambiare la firma da `(campaign, db, account_id, username)` a `(campaign_id, account_id, username=None)` e aprire dentro `AsyncSessionLocal()` (rileggendo la campagna), così è sicura in parallelo. Il batch (`_scrape_batch`) usa quella sessione locale.

```python
async def _scraping_accounts_of_campaign(campaign_id: str):
    """(account_id, username) degli account scraping/both attivi della campagna."""
    from app.database import AsyncSessionLocal
    from sqlalchemy import select
    from app.models.campaign_account import CampaignAccount
    from app.models.account import InstagramAccount, AccountStatus
    from app.utils.roles import SCRAPE_ROLES
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(InstagramAccount.id, InstagramAccount.username)
            .join(CampaignAccount, CampaignAccount.account_id == InstagramAccount.id)
            .where(
                CampaignAccount.campaign_id == campaign_id,
                CampaignAccount.is_active == True,  # noqa: E712
                CampaignAccount.role.in_(SCRAPE_ROLES),
                InstagramAccount.status == AccountStatus.active,
            )
        )).all()
    return [(r[0], r[1]) for r in rows]
```

- [ ] **Step 4: Implementa `run_pause_browser_all_accounts`**

```python
async def run_pause_browser_all_accounts(campaign_id: str) -> int:
    """Ogni account scraping della campagna fa la sua sessione scroll+batch in pausa.
    Parallelo con partenze scaglionate (offset random 1-3 min tra account, mai
    simultanei) e cap sui browser concorrenti. Ritorna i secondi totali spesi."""
    if not (settings.warmup_browse_enabled or settings.bio_browser_batch_enabled):
        return 0
    accounts = await _scraping_accounts_of_campaign(campaign_id)
    if not accounts:
        return 0
    start = time.monotonic()
    sem = asyncio.Semaphore(max(1, settings.max_concurrent_browsers))

    async def _one(account_id, username, idx):
        # Stagger: parte dopo idx * (1-3 min), cosi' non tutti nello stesso istante.
        if idx:
            await asyncio.sleep(random.uniform(60.0, 180.0) * idx)
        async with sem:
            await run_pause_browser_activity(campaign_id, account_id, username)

    await asyncio.gather(
        *[_one(a, u, i) for i, (a, u) in enumerate(accounts)],
        return_exceptions=True,  # un account che fallisce non blocca gli altri
    )
    return int(time.monotonic() - start)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `./venv/Scripts/python.exe -m pytest tests/test_pause_all_accounts.py -q`
Expected: PASS

- [ ] **Step 6: Cabla nel break di `scrape_bios`**

Sostituire nel break lungo:
```python
                        from app.services.browser_bio import run_pause_browser_activity
                        spent_seconds = await run_pause_browser_activity(
                            campaign, db, account.id, getattr(account, "username", None)
                        )
```
con:
```python
                        from app.services.browser_bio import run_pause_browser_all_accounts
                        spent_seconds = await run_pause_browser_all_accounts(campaign_id)
```

- [ ] **Step 7: Suite completa + commit**

Run: `./venv/Scripts/python.exe -m pytest tests/test_pause_all_accounts.py tests/test_browser_bio_mapping.py tests/test_bio_micro_yield.py tests/test_bio_error_no_infinite_loop.py -q`
Expected: PASS
```bash
git add backend/app/services/browser_bio.py backend/app/services/scrape_bios.py backend/tests/test_pause_all_accounts.py
git commit -m "feat(bio): attivita' browser in pausa su tutti gli account, scaglionati"
```

---

### Task 6: Migrazione DB + smoke test finale (a bot fermo)

- [ ] **Step 1: Applica la migrazione (bot FERMO, DB PROD Supabase)**

Run: `cd backend && ./venv/Scripts/python.exe -m scripts.migrate`
Expected: `019_current_session_cap` applicata, nessun errore lock/timeout.

- [ ] **Step 2: Import + suite completa**

Run: `./venv/Scripts/python.exe -m pytest tests/ -q`
Expected: nessun fallimento nei test toccati.

- [ ] **Step 3: Accendi le feature e riavvia (un solo restart)**

In `.env`: `WARMUP_BROWSE_ENABLED=true`, `BIO_BROWSER_BATCH_ENABLED=true` (+ `WARMUP_BROWSE_HEADLESS=false` per i primi test visivi).
Riavvia worker DM + cron + scraping. Lancia una campagna Fase Bio piccola, osserva nei log: delay variabili, `[BioBrowser] batch di N profili`, e al break le sessioni scaglionate su tutti gli account.

---

## Self-Review (fatto)

- **Copertura spec**: punto 1 (Task 4), punto 3 (Task 5), punto 4a (Task 1), punto 4b (Task 2-3). Punto 2 già fatto. Punti 5/6/7 esplicitamente out-of-scope.
- **Placeholder**: nessuno — codice completo in ogni step.
- **Coerenza tipi**: `pick_from_module`/`fetch_profile_app_like` (Task 1→3), `pick_session_cap` (Task 4), `run_pause_browser_all_accounts`/`_scraping_accounts_of_campaign` (Task 5) coerenti tra definizione e uso. `run_pause_browser_activity` cambia firma in Task 5 (campaign_id invece di campaign+db): aggiornare TUTTI i chiamati (il break di scrape_bios, Step 6).
- **Rischio aperto**: Task 3 ramo A vs B deciso dallo spike Task 2 (gate esplicito). Default ramo B (instagrapi-supported, basso rischio) se lo spike non conferma full_detail_info.

---

## Esito esecuzione (2026-07-05)

Tutti i task implementati (TDD, 22 test nuovi verdi; full suite 361 passed, 1 flaky pre-esistente `test_reservation`). Validazione live eseguita:
- **Task 4 / migrazione 021**: applicata (`alembic current = 021`, colonna `current_session_cap` presente in DB).
- **Task 2 spike `full_detail_info` → 404** "Endpoint does not exist" (instagrapi 2.3.0). **Ramo A escluso** → **ramo B confermato** (nessun cambio codice: era già il default).
- **Task 3 ramo B validato live**: `fetch_profile_app_like` ritorna il profilo (user_info app-like + `user_medias_v1` post, nessun crash).
- **Task 5 cattura browser validata live**: `BrowserSession` su @primeroa_adv7 (proxy `10.154.185.77:8080` → coerenza IP ok) → `_capture_web_profile_info` ritorna il JSON corretto, no 429 sulla singola chiamata.
- **Flag `.env` accesi** (`WARMUP_BROWSE_ENABLED`/`BIO_BROWSER_BATCH_ENABLED`/`WARMUP_BROWSE_HEADLESS=false`). ⚠️ I worker girano dal restart precedente → feature browser attive al **prossimo restart**; delay/cap/lookup app-like già live.
- **Aperto**: test volume browser-bio (rate `web_profile_info` prima del 429), merge branch su master.
