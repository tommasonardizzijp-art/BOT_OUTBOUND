# Unificazione macchina a stati import/scrape + guard dual-profilo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unificare la macchina a stati delle campagne `import` con quelle `scrape` (import = scrape − Fase Lista), risolvendo due bug di riavvio/requeue, e introdurre una guardia "2 profili distinti dedicati" prima di avviare scraping+DM in parallelo — per TUTTI i tipi di campagna, non solo import.

**Architecture:** Backend FastAPI (`backend/app/api/campaigns.py`, `backend/app/services/campaign_control.py`, `backend/app/utils/roles.py`) + frontend Next.js (`frontend/app/campaigns/[id]/page.tsx`, `frontend/lib/api.ts`, `frontend/lib/roles.ts`, `frontend/lib/types.ts`). Nessuna migrazione DB: si riusano `CampaignStatus.error`/`ready` esistenti e la colonna `campaign_accounts.role` esistente.

**Tech Stack:** Python 3.x, FastAPI, SQLAlchemy async, pytest + pytest-asyncio, Next.js/React, TypeScript.

## Global Constraints

- Worktree isolato: `D:\BOT OUTBOUND\.worktrees\import-scrape-unify` (branch `feature/import-scrape-unify`). Tutti i comandi girano da qui.
- Venv: usare `D:\BOT OUTBOUND\backend\venv\Scripts\python.exe` (il worktree non ha un proprio venv).
- Una sola suite pytest alla volta (DB sqlite condiviso + `phone_hmac` UNIQUE globale — vedi memoria `botoutbound-una-suite-pytest-alla-volta`). Mai lanciare pytest in parallelo su questo worktree.
- Baseline nota: 43 test falliscono già PRIMA di ogni modifica (moduli `test_phone_pseudonym.py`, `test_wa_models.py`, `test_wa_session.py` — subsistema WhatsApp non ancora chiuso, RuntimeError ambientale non correlato a questo lavoro). Non è una regressione introdotta da questo piano: NON provare a fixarli. Il criterio di successo per ogni task è "696+ passed, stesso set di 43 failed pre-esistenti, zero NUOVI failed".
- NON impostare `PLAYWRIGHT_BROWSERS_PATH=D:...` (corrompe profilo browser PoC-1).
- Ogni endpoint nuovo/modificato richiede test backend (pytest) PRIMA dell'implementazione (TDD).
- Diagrammi ASCII per la macchina a stati, se serve documentarla ulteriormente.
- Niente commit diretto su `main`: branch `feature/import-scrape-unify` + PR a fine lavoro.

---

## Diagramma stati (riferimento — nessuna modifica qui, solo per orientarsi)

```
SCRAPE:  draft → listing → listing_break → scraping/scraping_break/scraping_and_running → ready → running → paused → completed
IMPORT:  draft → scraping (risoluzione ImportedProfile pending) → ready/error → running → paused → completed
```

## File Structure

- Modifica: `backend/app/utils/roles.py` — nuove costanti `SCRAPE_ONLY_ROLES`, `DM_ONLY_ROLES`.
- Modifica: `backend/app/services/campaign_control.py` — nuovo helper `has_dedicated_scrape_and_dm_accounts`; guard applicata al ramo `scraping_and_running` di `resume_campaign_control`.
- Modifica: `backend/app/api/campaigns.py` — fix `/reset` (sintomo A), nuovo endpoint `/import-retry-failed` (sintomo B), sblocco `/start-dm-auto` per import + guard dual-profilo.
- Test: `backend/tests/test_campaigns_import_reset.py` (nuovo), `backend/tests/test_campaigns_import_retry_failed.py` (nuovo), `backend/tests/test_dual_profile_guard.py` (nuovo).
- Modifica: `frontend/lib/roles.ts` — `isScrapeOnly`, `isDmOnly`.
- Modifica: `frontend/lib/api.ts` — `importRetryFailed(id)`.
- Modifica: `frontend/lib/types.ts` — se serve, nessun nuovo status (si riusano `error`/`ready`).
- Modifica: `frontend/app/campaigns/[id]/page.tsx` — bottone requeue bulk nel pannello import, sblocco bottone "Avvia DM ora" per import, disabled-state con tooltip quando manca il secondo profilo dedicato.

---

### Task 1: Costanti ruoli dedicati + helper guard dual-profilo (backend)

**Files:**
- Modify: `backend/app/utils/roles.py`
- Modify: `backend/app/services/campaign_control.py`
- Test: `backend/tests/test_dual_profile_guard.py`

**Interfaces:**
- Produces: `SCRAPE_ONLY_ROLES: tuple[str,...]`, `DM_ONLY_ROLES: tuple[str,...]` in `app.utils.roles`; `has_dedicated_scrape_and_dm_accounts(db, campaign_id) -> bool` in `app.services.campaign_control`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_dual_profile_guard.py
import pytest
from app.services.campaign_control import has_dedicated_scrape_and_dm_accounts
from app.models.campaign_account import CampaignAccount
from app.models.account import InstagramAccount, AccountStatus


async def _make_account(db, role, status=AccountStatus.active):
    acc = InstagramAccount(username=f"acc_{role}_{id(role)}", status=status, session_data="{}")
    db.add(acc)
    await db.flush()
    return acc


async def _assign(db, campaign_id, account, role, is_active=True):
    ca = CampaignAccount(campaign_id=campaign_id, account_id=account.id, role=role, is_active=is_active)
    db.add(ca)
    await db.flush()
    return ca


@pytest.mark.asyncio
async def test_no_accounts_returns_false(db_session, campaign_factory):
    campaign = await campaign_factory(db_session)
    assert await has_dedicated_scrape_and_dm_accounts(db_session, campaign.id) is False


@pytest.mark.asyncio
async def test_single_both_account_returns_false(db_session, campaign_factory):
    """Un solo profilo role='both' NON basta: non e' dedicato ne' a scrape ne' a dm da solo."""
    campaign = await campaign_factory(db_session)
    acc = await _make_account(db_session, "both")
    await _assign(db_session, campaign.id, acc, "both")
    await db_session.commit()
    assert await has_dedicated_scrape_and_dm_accounts(db_session, campaign.id) is False


@pytest.mark.asyncio
async def test_two_both_accounts_returns_false(db_session, campaign_factory):
    """Due profili 'both' non contano come dedicati: nessuno dei due e' scraping-only o dm-only."""
    campaign = await campaign_factory(db_session)
    acc1 = await _make_account(db_session, "both")
    acc2 = await _make_account(db_session, "both")
    await _assign(db_session, campaign.id, acc1, "both")
    await _assign(db_session, campaign.id, acc2, "both")
    await db_session.commit()
    assert await has_dedicated_scrape_and_dm_accounts(db_session, campaign.id) is False


@pytest.mark.asyncio
async def test_one_scraping_only_one_dm_only_returns_true(db_session, campaign_factory):
    campaign = await campaign_factory(db_session)
    acc1 = await _make_account(db_session, "scraping")
    acc2 = await _make_account(db_session, "dm")
    await _assign(db_session, campaign.id, acc1, "scraping")
    await _assign(db_session, campaign.id, acc2, "dm")
    await db_session.commit()
    assert await has_dedicated_scrape_and_dm_accounts(db_session, campaign.id) is True


@pytest.mark.asyncio
async def test_scraping_only_present_but_dm_only_inactive_returns_false(db_session, campaign_factory):
    campaign = await campaign_factory(db_session)
    acc1 = await _make_account(db_session, "scraping")
    acc2 = await _make_account(db_session, "dm")
    await _assign(db_session, campaign.id, acc1, "scraping")
    await _assign(db_session, campaign.id, acc2, "dm", is_active=False)
    await db_session.commit()
    assert await has_dedicated_scrape_and_dm_accounts(db_session, campaign.id) is False


@pytest.mark.asyncio
async def test_inbox_scraping_and_inbox_dm_count_as_dedicated(db_session, campaign_factory):
    """inbox_scraping/inbox_dm sono comunque a singola capability (scrape XOR dm), inbox e' ortogonale."""
    campaign = await campaign_factory(db_session)
    acc1 = await _make_account(db_session, "inbox_scraping")
    acc2 = await _make_account(db_session, "inbox_dm")
    await _assign(db_session, campaign.id, acc1, "inbox_scraping")
    await _assign(db_session, campaign.id, acc2, "inbox_dm")
    await db_session.commit()
    assert await has_dedicated_scrape_and_dm_accounts(db_session, campaign.id) is True
```

Verifica prima le fixture disponibili in `backend/tests/conftest.py` (`db_session`, eventuale `campaign_factory`): se `campaign_factory` non esiste, crea la campagna inline con `Campaign(id=..., name=..., source_type="scrape", ...)` seguendo il pattern di un test esistente in `backend/tests/test_campaigns_api.py` o simile (cercalo con grep prima di scrivere il test, per riusare i default richiesti dal modello `Campaign`).

- [ ] **Step 2: Run test to verify it fails**

Run: `"D:\BOT OUTBOUND\backend\venv\Scripts\python.exe" -m pytest backend/tests/test_dual_profile_guard.py -v` (dalla root del worktree)
Expected: FAIL con `ImportError: cannot import name 'has_dedicated_scrape_and_dm_accounts'`

- [ ] **Step 3: Implementazione minima — costanti ruoli**

In `backend/app/utils/roles.py`, dopo `INBOX_ROLES` (riga 37):

```python
# Puo' fare SOLO bio scraping, mai DM (capability singola, esclude 'both'/'inbox_both').
SCRAPE_ONLY_ROLES: tuple[str, ...] = ("scraping", "inbox_scraping")

# Puo' fare SOLO DM, mai bio scraping (capability singola, esclude 'both'/'inbox_both').
DM_ONLY_ROLES: tuple[str, ...] = ("dm", "inbox_dm")
```

- [ ] **Step 4: Implementazione minima — helper guard**

In `backend/app/services/campaign_control.py`, import aggiornato:

```python
from app.utils.roles import SCRAPE_ROLES, DM_ROLES, INBOX_ROLES, SCRAPE_ONLY_ROLES, DM_ONLY_ROLES
```

Aggiungi dopo `has_active_role_account` (dopo riga 117):

```python
async def has_dedicated_scrape_and_dm_accounts(
    db: AsyncSession,
    campaign_id: str,
) -> bool:
    """True se la campagna ha ALMENO 1 account attivo dedicato SOLO allo scraping
    (role in SCRAPE_ONLY_ROLES) E ALMENO 1 account attivo DIVERSO dedicato SOLO ai DM
    (role in DM_ONLY_ROLES). Un account role='both' non soddisfa nessuno dei due bucket:
    avviare scraping+DM in parallelo su un solo profilo 'both' fa scrapare e mandare DM
    allo stesso account nella stessa finestra, il pattern che genera i checkpoint IG
    (vedi memoria botoutbound-checkpoint-pattern-api / botoutbound-antidetect-protocollo-rigido).
    """
    has_scrape_only = await has_active_role_account(db, campaign_id, SCRAPE_ONLY_ROLES)
    if not has_scrape_only:
        return False
    return await has_active_role_account(db, campaign_id, DM_ONLY_ROLES)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `"D:\BOT OUTBOUND\backend\venv\Scripts\python.exe" -m pytest backend/tests/test_dual_profile_guard.py -v`
Expected: PASS (6/6)

- [ ] **Step 6: Commit**

```bash
git add backend/app/utils/roles.py backend/app/services/campaign_control.py backend/tests/test_dual_profile_guard.py
git commit -m "feat: guard 2-profili-dedicati per scraping+DM in parallelo"
```

---

### Task 2: Applicare la guard a `start-dm-auto` e sbloccarlo per import

**Files:**
- Modify: `backend/app/api/campaigns.py:876-955` (endpoint `start_dm_auto`)
- Test: `backend/tests/test_campaigns_start_dm_auto.py` (nuovo, o estendi un file esistente se `start-dm-auto` è già testato — cerca con grep `start-dm-auto` o `start_dm_auto` in `backend/tests/` prima di creare un file duplicato)

**Interfaces:**
- Consumes: `has_dedicated_scrape_and_dm_accounts(db, campaign_id)` da Task 1.
- Produces: nessuna nuova funzione pubblica: modifica di comportamento dell'endpoint esistente `POST /{campaign_id}/start-dm-auto`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_campaigns_start_dm_auto.py
import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.models.campaign import Campaign, CampaignStatus
from app.models.campaign_account import CampaignAccount
from app.models.account import InstagramAccount, AccountStatus


async def _account(db, role, status=AccountStatus.active):
    acc = InstagramAccount(username=f"acc_{role}_{id(role)}", status=status, session_data="{}")
    db.add(acc)
    await db.flush()
    return acc


@pytest.mark.asyncio
async def test_start_dm_auto_import_blocked_with_single_both_account(db_session, campaign_factory):
    """Import in scraping, un solo account 'both': deve restare bloccato (nessun profilo dedicato)."""
    campaign = await campaign_factory(db_session, source_type="import", status=CampaignStatus.scraping,
                                       messaging_enabled=True, base_message_template="ciao " * 5)
    acc = await _account(db_session, "both")
    db_session.add(CampaignAccount(campaign_id=campaign.id, account_id=acc.id, role="both", is_active=True))
    await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/campaigns/{campaign.id}/start-dm-auto")
    assert resp.status_code == 400
    assert "dedicat" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_start_dm_auto_import_allowed_with_two_dedicated_accounts(db_session, campaign_factory, monkeypatch):
    """Import in scraping, un account scraping-only + un account dm-only: deve passare a scraping_and_running."""
    campaign = await campaign_factory(db_session, source_type="import", status=CampaignStatus.scraping,
                                       messaging_enabled=True, base_message_template="ciao " * 5)
    acc_scrape = await _account(db_session, "scraping")
    acc_dm = await _account(db_session, "dm")
    db_session.add(CampaignAccount(campaign_id=campaign.id, account_id=acc_scrape.id, role="scraping", is_active=True))
    db_session.add(CampaignAccount(campaign_id=campaign.id, account_id=acc_dm.id, role="dm", is_active=True))
    await db_session.commit()

    async def _ok(*a, **kw):
        return True
    monkeypatch.setattr("app.api.campaigns._check_redis_reachable", _ok)
    async def _fake_enqueue(*a, **kw):
        return 1
    monkeypatch.setattr("app.services.work_enqueue.enqueue_campaign_run", _fake_enqueue)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/campaigns/{campaign.id}/start-dm-auto")
    assert resp.status_code == 200
    assert resp.json()["status"] == "scraping_and_running"
```

Prima di scrivere, cerca (`grep -r "start-dm-auto\|start_dm_auto" backend/tests/`) se esiste già un file di test per questo endpoint e il pattern per mockare `_check_redis_reachable`/`enqueue_campaign_run` usato altrove — riusa quel pattern esatto invece di inventarne uno nuovo (i mock path devono combaciare con come la funzione viene importata in `campaigns.py`, verifica con `grep -n "_check_redis_reachable\|enqueue_campaign_run" backend/app/api/campaigns.py`).

- [ ] **Step 2: Run test to verify it fails**

Run: `"D:\BOT OUTBOUND\backend\venv\Scripts\python.exe" -m pytest backend/tests/test_campaigns_start_dm_auto.py -v`
Expected: FAIL — il primo test passa già (import è comunque bloccato oggi, ma con messaggio diverso, "DM in parallelo non disponibile per campagne import" invece di menzionare "dedicat"); il secondo FAIL con 400 perché oggi import è bloccato sempre.

- [ ] **Step 3: Implementazione minima**

In `backend/app/api/campaigns.py`, sostituisci il blocco `if campaign.source_type == "import": raise HTTPException(...)` (righe 888-896) — RIMUOVILO interamente (import ora può fare scraping+DM in parallelo, come scrape). Poi, subito dopo il check esistente `dm_ca` (righe 909-925), aggiungi la guard dual-profilo PRIMA della transizione di stato:

```python
    if not dm_ca.scalar_one_or_none():
        raise HTTPException(
            status_code=400,
            detail="Assegna almeno un account attivo con ruolo 'dm' o 'entrambi' prima di avviare i DM"
        )

    if not await has_dedicated_scrape_and_dm_accounts(db, campaign_id):
        raise HTTPException(
            status_code=400,
            detail="Servono almeno 2 profili distinti: uno dedicato SOLO allo scraping "
            "(ruolo 'scraping') e uno dedicato SOLO ai DM (ruolo 'dm'). Un profilo 'entrambi' "
            "da solo non basta: farebbe scraping e DM in parallelo sullo stesso account, "
            "generando checkpoint Instagram."
        )
```

Aggiungi l'import in cima al file: `from app.services.campaign_control import has_dedicated_scrape_and_dm_accounts` (verifica se `campaign_control` è già importato in `campaigns.py` con `grep -n "campaign_control" backend/app/api/campaigns.py` — se sì, aggiungi solo il nome alla import esistente).

- [ ] **Step 4: Run test to verify it passes**

Run: `"D:\BOT OUTBOUND\backend\venv\Scripts\python.exe" -m pytest backend/tests/test_campaigns_start_dm_auto.py -v`
Expected: PASS (2/2)

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/campaigns.py backend/tests/test_campaigns_start_dm_auto.py
git commit -m "feat: sblocca scraping+DM parallelo per import, guard 2-profili-dedicati"
```

---

### Task 3: Stessa guard su `resume_campaign_control` (ramo scraping_and_running)

**Files:**
- Modify: `backend/app/services/campaign_control.py:208-227`
- Test: `backend/tests/test_campaign_control_resume.py` (nuovo, o estendi esistente — grep `resume_campaign_control` in `backend/tests/` prima)

**Interfaces:**
- Consumes: `has_dedicated_scrape_and_dm_accounts` da Task 1.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_campaign_control_resume.py (aggiungi se il file esiste già, altrimenti crealo)
import pytest
from app.services.campaign_control import resume_campaign_control, CampaignControlError
from app.models.campaign import Campaign, CampaignStatus
from app.models.campaign_account import CampaignAccount
from app.models.account import InstagramAccount, AccountStatus


async def _account(db, role, status=AccountStatus.active):
    acc = InstagramAccount(username=f"acc_{role}_{id(role)}", status=status, session_data="{}")
    db.add(acc)
    await db.flush()
    return acc


@pytest.mark.asyncio
async def test_resume_parallel_import_blocked_without_dedicated_accounts(db_session, campaign_factory, monkeypatch):
    campaign = await campaign_factory(
        db_session, source_type="import", status=CampaignStatus.paused,
        auto_generate=True, messaging_enabled=True, base_message_template="ciao " * 5,
        scrape_completed_at=None,
    )
    acc = await _account(db_session, "both")
    db_session.add(CampaignAccount(campaign_id=campaign.id, account_id=acc.id, role="both", is_active=True))
    await db_session.commit()

    async def _ok(*a, **kw):
        return True
    monkeypatch.setattr("app.services.campaign_control.check_redis_reachable", _ok)
    async def _no_halt(*a, **kw):
        return None
    monkeypatch.setattr("app.services.campaign_control.ensure_bot_accepts_work", _no_halt)

    with pytest.raises(CampaignControlError, match="dedicat"):
        await resume_campaign_control(db_session, campaign.id, by="test", enqueue=False)
```

Verifica i campi obbligatori del fixture `campaign_factory` (o del modello `Campaign` se non esiste una factory) con grep prima di scrivere: `grep -n "class Campaign" -A 40 backend/app/models/campaign.py`.

- [ ] **Step 2: Run test to verify it fails**

Run: `"D:\BOT OUTBOUND\backend\venv\Scripts\python.exe" -m pytest backend/tests/test_campaign_control_resume.py -v`
Expected: FAIL — oggi con un solo account `both` il resume passa (nessuna guard), quindi non solleva `CampaignControlError`.

- [ ] **Step 3: Implementazione minima**

In `backend/app/services/campaign_control.py`, dentro `resume_campaign_control`, sostituisci righe 217-227:

```python
            has_dm_account = await has_active_role_account(db, campaign_id, DM_ROLES)
            if campaign.auto_generate:
                ensure_campaign_can_send_messages(campaign)
            if campaign.auto_generate and not has_dm_account:
                raise CampaignControlError(
                    "auto_generate attivo ma nessun account DM/both: "
                    "assegna un account DM o disattiva auto_generate."
                )
            if campaign.auto_generate and has_dm_account:
                if not await has_dedicated_scrape_and_dm_accounts(db, campaign_id):
                    raise CampaignControlError(
                        "Servono almeno 2 profili distinti: uno dedicato SOLO allo scraping "
                        "e uno dedicato SOLO ai DM. Un profilo 'entrambi' da solo non basta."
                    )
                campaign.status = CampaignStatus.scraping_and_running
                action = "campaign_resumed_parallel"
            else:
                campaign.status = CampaignStatus.scraping
                action = "scrape_resumed"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `"D:\BOT OUTBOUND\backend\venv\Scripts\python.exe" -m pytest backend/tests/test_campaign_control_resume.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/campaign_control.py backend/tests/test_campaign_control_resume.py
git commit -m "feat: guard 2-profili-dedicati anche su resume scraping_and_running"
```

---

### Task 4: Fix `/reset` — sintomo A (import parzialmente risolto non ripartibile)

**Files:**
- Modify: `backend/app/api/campaigns.py:807-873` (endpoint `reset_campaign`)
- Test: `backend/tests/test_campaigns_import_reset.py` (nuovo)

**Interfaces:**
- Produces: comportamento modificato di `POST /{campaign_id}/reset` per `source_type=="import"`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_campaigns_import_reset.py
import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.models.campaign import CampaignStatus
from app.models.imported_profile import ImportedProfile
from app.models.follower import Follower, FollowerStatus


@pytest.mark.asyncio
async def test_reset_import_with_pending_rows_goes_to_error_not_ready(db_session, campaign_factory):
    """Import con follower gia' risolti MA righe pending ancora da lavorare: reset deve
    portare a 'error' (start-scrape la riprende), non a 'ready' (bloccato)."""
    campaign = await campaign_factory(db_session, source_type="import", status=CampaignStatus.paused)
    db_session.add(Follower(campaign_id=campaign.id, username="resolved_one", status=FollowerStatus.bio_scraped))
    db_session.add(ImportedProfile(campaign_id=campaign.id, username="resolved_one", status="resolved"))
    db_session.add(ImportedProfile(campaign_id=campaign.id, username="still_pending", status="pending"))
    await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/campaigns/{campaign.id}/reset")
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"


@pytest.mark.asyncio
async def test_reset_import_fully_resolved_goes_to_ready(db_session, campaign_factory):
    """Import completamente risolto (nessuna riga pending): reset va a 'ready' come oggi."""
    campaign = await campaign_factory(db_session, source_type="import", status=CampaignStatus.paused)
    db_session.add(Follower(campaign_id=campaign.id, username="resolved_one", status=FollowerStatus.bio_scraped))
    db_session.add(ImportedProfile(campaign_id=campaign.id, username="resolved_one", status="resolved"))
    await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/campaigns/{campaign.id}/reset")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


@pytest.mark.asyncio
async def test_reset_import_no_followers_no_pending_goes_to_draft(db_session, campaign_factory):
    """Import senza alcun follower risolto e senza righe pending (es. tutte not_found/error):
    reset va a draft E rimette a pending le righe not_found/error per poterle ritentare."""
    campaign = await campaign_factory(db_session, source_type="import", status=CampaignStatus.paused)
    db_session.add(ImportedProfile(campaign_id=campaign.id, username="bad_one", status="not_found"))
    await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/campaigns/{campaign.id}/reset")
    assert resp.status_code == 200
    assert resp.json()["status"] == "draft"
```

Prima di scrivere, controlla i campi obbligatori di `ImportedProfile` (`grep -n "class ImportedProfile" -A 25 backend/app/models/*.py`) e adegua i test se ne mancano (es. `ig_user_id`, timestamps con default).

- [ ] **Step 2: Run test to verify it fails**

Run: `"D:\BOT OUTBOUND\backend\venv\Scripts\python.exe" -m pytest backend/tests/test_campaigns_import_reset.py -v`
Expected: FAIL sul primo test (oggi va a `ready` perché `actual_count>0`, non considera le righe pending).

- [ ] **Step 3: Implementazione minima**

In `backend/app/api/campaigns.py`, sostituisci il blocco righe 825-867:

```python
    # Count actual followers in DB (they're kept, just status-reset)
    from sqlalchemy import func as sa_func
    actual_count = await db.scalar(
        select(sa_func.count(Follower.id)).where(Follower.campaign_id == campaign_id)
    ) or 0

    is_import = campaign.source_type == "import"
    unresolved_count = 0
    if is_import:
        unresolved_count = await db.scalar(
            select(sa_func.count(ImportedProfile.id)).where(
                ImportedProfile.campaign_id == campaign_id,
                ImportedProfile.status.in_(("pending", "resolving")),
            )
        ) or 0

    # Reset landing status:
    # - scrape → draft (si ri-scrappa la pagina target)
    # - import CON righe ancora da risolvere (pending/resolving) → error: start-scrape
    #   (draft|error) riprende la risoluzione senza perdere i follower gia' risolti.
    #   Prima di questo fix andava a 'ready' se actual_count>0, bloccando per sempre
    #   il resto della lista (sintomo A, vedi memoria botoutbound-campagne-import-macchina-stati).
    # - import SENZA righe pending e CON follower risolti → ready: si riparte dai DM.
    # - import SENZA righe pending e SENZA follower → draft + righe not_found/error
    #   rimesse a pending (sotto) per rilanciare da zero.
    if is_import and unresolved_count > 0:
        campaign.status = CampaignStatus.error
        campaign.scrape_completed_at = None
    elif is_import and actual_count > 0:
        campaign.status = CampaignStatus.ready
        campaign.scrape_completed_at = datetime.utcnow()
    else:
        campaign.status = CampaignStatus.draft
        campaign.scrape_completed_at = None
    campaign.total_followers = actual_count
    campaign.messages_sent = 0
    campaign.messages_failed = 0
    campaign.messages_pending = actual_count
    campaign.started_at = None
    campaign.completed_at = None
    campaign.auto_generate = False
    campaign.scrape_break_until = None
    campaign.scrape_break_prev_status = None
    campaign.updated_at = datetime.utcnow()

    # BUG-NEW-05: delete old messages so the campaign starts clean
    await db.execute(delete(Message).where(Message.campaign_id == campaign_id))

    # Reset follower statuses and clear any stale locks
    await db.execute(
        update(Follower)
        .where(Follower.campaign_id == campaign_id)
        .values(status=FollowerStatus.bio_scraped, locked_by_account_id=None, locked_at=None)
    )

    # Import senza lead risolti e senza righe pending: rimetti not_found/error a
    # pending cosi' la risoluzione puo' ripartire da zero (start-scrape la consuma).
    if is_import and actual_count == 0 and unresolved_count == 0:
        await db.execute(
            update(ImportedProfile)
            .where(ImportedProfile.campaign_id == campaign_id)
            .values(status="pending", ig_user_id=None, error=None)
        )
```

Nota: la variabile `is_import` era già dichiarata più sotto nel codice originale (riga 831) — questa versione la sposta più in alto e la riusa, verifica che non resti una dichiarazione duplicata più giù nel file.

- [ ] **Step 4: Run test to verify it passes**

Run: `"D:\BOT OUTBOUND\backend\venv\Scripts\python.exe" -m pytest backend/tests/test_campaigns_import_reset.py -v`
Expected: PASS (3/3)

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/campaigns.py backend/tests/test_campaigns_import_reset.py
git commit -m "fix: reset import con righe pending torna a error (riavviabile), non ready (bloccato)"
```

---

### Task 5: Nuovo endpoint `/import-retry-failed` — sintomo B (requeue bulk)

**Files:**
- Modify: `backend/app/api/campaigns.py` (nuovo endpoint, subito dopo `import_status`, prima di `start_scrape` — dopo riga 450)
- Test: `backend/tests/test_campaigns_import_retry_failed.py` (nuovo)

**Interfaces:**
- Produces: `POST /{campaign_id}/import-retry-failed` → `CampaignResponse`, requeue bulk (non per-riga, come da decisione).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_campaigns_import_retry_failed.py
import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.models.campaign import CampaignStatus
from app.models.imported_profile import ImportedProfile
from app.models.follower import Follower, FollowerStatus


@pytest.mark.asyncio
async def test_retry_failed_requeues_not_found_and_error_rows(db_session, campaign_factory):
    campaign = await campaign_factory(db_session, source_type="import", status=CampaignStatus.ready)
    db_session.add(Follower(campaign_id=campaign.id, username="ok_one", status=FollowerStatus.bio_scraped))
    db_session.add(ImportedProfile(campaign_id=campaign.id, username="ok_one", status="resolved"))
    db_session.add(ImportedProfile(campaign_id=campaign.id, username="bad_a", status="not_found"))
    db_session.add(ImportedProfile(campaign_id=campaign.id, username="bad_b", status="error", error="timeout"))
    db_session.add(ImportedProfile(campaign_id=campaign.id, username="private_one", status="private"))
    await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/campaigns/{campaign.id}/import-retry-failed")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"  # ora ci sono 2 righe pending -> riavviabile

    rows = await db_session.execute(
        __import__("sqlalchemy").select(ImportedProfile.username, ImportedProfile.status)
        .where(ImportedProfile.campaign_id == campaign.id)
    )
    by_username = {u: s for u, s in rows.all()}
    assert by_username["bad_a"] == "pending"
    assert by_username["bad_b"] == "pending"
    assert by_username["private_one"] == "private"  # 'private' NON e' un fallimento da ritentare
    assert by_username["ok_one"] == "resolved"


@pytest.mark.asyncio
async def test_retry_failed_on_non_import_campaign_returns_400(db_session, campaign_factory):
    campaign = await campaign_factory(db_session, source_type="scrape", status=CampaignStatus.ready)
    await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/campaigns/{campaign.id}/import-retry-failed")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_retry_failed_with_nothing_to_retry_returns_400(db_session, campaign_factory):
    campaign = await campaign_factory(db_session, source_type="import", status=CampaignStatus.ready)
    db_session.add(ImportedProfile(campaign_id=campaign.id, username="ok_one", status="resolved"))
    await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/campaigns/{campaign.id}/import-retry-failed")
    assert resp.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `"D:\BOT OUTBOUND\backend\venv\Scripts\python.exe" -m pytest backend/tests/test_campaigns_import_retry_failed.py -v`
Expected: FAIL con 404 (endpoint non esiste ancora)

- [ ] **Step 3: Implementazione minima**

In `backend/app/api/campaigns.py`, subito dopo la funzione `import_status` (dopo riga 450, prima di `start_scrape`):

```python
@router.post("/{campaign_id}/import-retry-failed", response_model=CampaignResponse)
async def import_retry_failed(campaign_id: str, db: AsyncSession = Depends(get_db)):
    """Rimette in coda TUTTE le righe imported_profiles in stato not_found/error
    (requeue bulk, non per-riga — 'private' resta escluso: e' un esito definitivo,
    non un fallimento tecnico da ritentare)."""
    campaign = await _get_or_404(campaign_id, db)
    if campaign.source_type != "import":
        raise HTTPException(status_code=400, detail="La campagna non è di tipo 'import'")

    failed_count = await db.scalar(
        select(func.count(ImportedProfile.id)).where(
            ImportedProfile.campaign_id == campaign_id,
            ImportedProfile.status.in_(("not_found", "error")),
        )
    ) or 0
    if failed_count == 0:
        raise HTTPException(status_code=400, detail="Nessun profilo fallito da ritentare")

    await db.execute(
        update(ImportedProfile)
        .where(
            ImportedProfile.campaign_id == campaign_id,
            ImportedProfile.status.in_(("not_found", "error")),
        )
        .values(status="pending", ig_user_id=None, error=None)
    )

    # Se la campagna era ferma su ready/completed/paused, sblocca il riavvio:
    # ora ci sono righe pending, start-scrape le riprende (richiede draft/error).
    if campaign.status not in (CampaignStatus.draft, CampaignStatus.error):
        campaign.status = CampaignStatus.error
    campaign.updated_at = datetime.utcnow()

    db.add(
        ActivityLog(
            campaign_id=campaign.id,
            action="import_retry_failed",
            details=json.dumps({"requeued": failed_count}),
        )
    )
    await db.commit()
    await db.refresh(campaign)
    return await _enrich_campaign(campaign, db, include_today=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `"D:\BOT OUTBOUND\backend\venv\Scripts\python.exe" -m pytest backend/tests/test_campaigns_import_retry_failed.py -v`
Expected: PASS (3/3)

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/campaigns.py backend/tests/test_campaigns_import_retry_failed.py
git commit -m "feat: endpoint requeue bulk profili import falliti (not_found/error)"
```

---

### Task 6: Frontend — requeue bulk, sblocco scraping+DM per import, guard UI dual-profilo

**Files:**
- Modify: `frontend/lib/roles.ts`
- Modify: `frontend/lib/api.ts`
- Modify: `frontend/app/campaigns/[id]/page.tsx`

**Interfaces:**
- Consumes: endpoint `POST /{id}/import-retry-failed` da Task 5, semantica guard 2-profili da Task 2/3.
- Produces: `api.campaigns.importRetryFailed(id)`, `isScrapeOnly(role)`, `isDmOnly(role)` in `roles.ts`.

- [ ] **Step 1: `roles.ts` — helper dedicati**

In `frontend/lib/roles.ts`, dopo `INBOX_ROLES`:

```typescript
export const SCRAPE_ONLY_ROLES: AccountRole[] = ['scraping', 'inbox_scraping']
export const DM_ONLY_ROLES: AccountRole[] = ['dm', 'inbox_dm']

export const isScrapeOnly = (role?: AccountRole | null) => SCRAPE_ONLY_ROLES.includes((role ?? 'both') as AccountRole)
export const isDmOnly = (role?: AccountRole | null) => DM_ONLY_ROLES.includes((role ?? 'both') as AccountRole)
```

- [ ] **Step 2: `api.ts` — nuova chiamata**

Trova la definizione di `importStatus` in `frontend/lib/api.ts:184` e aggiungi subito dopo, stesso stile (stesso pattern di `api.campaigns.retryFailed` usato da `handleRetryFailed` nella pagina — cercalo con grep per copiarne esattamente la firma):

```typescript
importRetryFailed: (id: string) => apiFetch(`/campaigns/${id}/import-retry-failed`, { method: 'POST' }),
```

(Adegua il nome della funzione helper `apiFetch`/`fetchJSON`/altro al pattern realmente usato nel file — leggilo prima di scrivere.)

- [ ] **Step 3: `page.tsx` — bottone requeue nel pannello import**

Nel blocco pannello import (righe 996-1009), dopo la griglia dei conteggi, aggiungi il bottone visibile solo se `importStatus.not_found + importStatus.error > 0`:

```tsx
{campaign.source_type === 'import' && importStatus && (
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
    {(importStatus.not_found + importStatus.error) > 0 && (
      <Button size="sm" variant="outline" className="border-orange-700 text-orange-400 hover:bg-orange-900/20"
        onClick={() => action(() => api.campaigns.importRetryFailed(id))} disabled={loadingAction}
        title="Rimette in coda i profili non trovati/in errore per un nuovo tentativo di risoluzione">
        {loadingAction ? <Loader2 className="w-4 h-4 animate-spin" /> : <><RotateCcw className="w-4 h-4 mr-1" />Ritenta falliti ({importStatus.not_found + importStatus.error})</>}
      </Button>
    )}
  </div>
)}
```

- [ ] **Step 4: `page.tsx` — sblocco bottone "Avvia DM ora" per import + guard visiva 2 profili**

Sostituisci il blocco righe 906-913:

```tsx
{/* Avvia DM in parallelo mentre scraping/risoluzione gira. Richiede 2 profili
    dedicati distinti (uno scraping-only, uno dm-only) — lo stesso profilo 'both'
    non basta, farebbe scraping e DM insieme sull'account, causa checkpoint IG. */}
{campaign.messaging_enabled && campaign.status === 'scraping' && !campaign.scrape_completed_at && (() => {
  const hasScrapeOnly = campaignAccounts?.some(ca => ca.is_active && isScrapeOnly(ca.role)) ?? false
  const hasDmOnly = campaignAccounts?.some(ca => ca.is_active && isDmOnly(ca.role)) ?? false
  const dualReady = hasScrapeOnly && hasDmOnly
  return (
    <Button size="sm" className="bg-green-700 hover:bg-green-600 text-white disabled:opacity-40"
      onClick={() => action(() => api.campaigns.startDmAuto(id))} disabled={loadingAction || !dualReady}
      title={dualReady
        ? 'Avvia invio DM mentre lo scraping continua in background (auto-gen)'
        : 'Servono 2 profili distinti: uno dedicato SOLO allo scraping e uno dedicato SOLO ai DM (ruolo "entrambi" da solo non basta)'}>
      {loadingAction ? <Loader2 className="w-4 h-4 animate-spin" /> : <><Zap className="w-4 h-4 mr-1" />Avvia DM ora</>}
    </Button>
  )
})()}
```

Nota: questo blocco rimuove il precedente `campaign.source_type !== 'import'` — ora vale per entrambi i tipi. Aggiungi l'import di `isScrapeOnly, isDmOnly` in cima al file dove già si importa `canDm` da `@/lib/roles`.

- [ ] **Step 5: Verifica manuale build**

Run (dalla root frontend): `npm run build` oppure `npx tsc --noEmit` se il progetto usa quel comando per il typecheck — verifica in `package.json` quale script esiste prima di lanciare.
Expected: nessun errore TypeScript.

- [ ] **Step 6: Commit**

```bash
git add frontend/lib/roles.ts frontend/lib/api.ts "frontend/app/campaigns/[id]/page.tsx"
git commit -m "feat(ui): requeue bulk import falliti, sblocco scraping+DM import con guard 2-profili"
```

---

## Fase 3/4 — QA e chiusura modulo (obbligatorio, skill sviluppo-modulo)

Dopo Task 6:
1. QA agent: rilancia l'intera suite pytest (`"D:\BOT OUTBOUND\backend\venv\Scripts\python.exe" -m pytest -q` dal worktree) — criterio di successo: stesso set di 43 failed pre-esistenti (WhatsApp), zero nuovi failed, tutti i nuovi test di questo piano verdi.
2. QA agent E2E browser (Playwright, `PLAYWRIGHT_BROWSERS_PATH=D:\dev\.playwright-browsers`) su almeno: creazione campagna import → upload lista con alcuni username inesistenti → avvio → verifica risoluzione parziale → reset → verifica stato `error` e bottone "Riprendi risoluzione" visibile → click → verifica ripresa → requeue falliti → verifica pannello aggiornato.
3. Lista test manuali UI (minimo 20) + lista adversarial (minimo 30, categorie da SKILL.md: concorrenza reset+requeue in parallelo, doppio click sul bottone requeue, campagna eliminata a metà risoluzione, 0 account assegnati, requeue su campagna con 0 falliti, guard 2-profili con account disattivato a runtime, ecc.) salvate in `.superpowers/sdd/qa-import-scrape-unify-tests.md` e `qa-import-scrape-unify-adversarial.md` nel worktree, partendo dai modelli in `d:\dev\thevista-app-magazzino\.superpowers\sdd\`.
4. Fix loop fino al 100%.
5. Final whole-branch review (`superpowers:requesting-code-review`).
6. PR verso `main` (mai push diretto).

**Nessun collaudo di Tommaso per singola milestone — solo a MVP dell'intero cantiere (che qui coincide con la chiusura di questo piano).**
