# Fase 0 — Urgent Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminare i rischi critici/urgenti di sicurezza e di transizione di stato del bot outbound (segreti esposti, auth disattivabile, lead bruciati su crash, getter ricorsivo nel fingerprint, campagne uccise dal restart API, config UI ignorate) senza introdurre regressioni.

**Architecture:** Modifiche chirurgiche su moduli esistenti. Nessun refactor strutturale in Fase 0 (queue redesign e `contact_reservations` sono Fase 1/2 in `AUDIT_UNIFICATO.md`). Decisioni di approccio già fissate: reservation = fix minimo; auth = rimozione legacy mode; queue = rinviata.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2 async, ARQ, Patchright, Pydantic Settings, Next.js. DB attivo: Postgres/Supabase.

**Convenzione di verifica (no test runner affidabile finché Task 1 non completa):**
- Backend sintassi: da `backend/`, `./venv/Scripts/python -m compileall app` → atteso `Listing ... ` senza `SyntaxError`.
- Test mirati: disponibili solo dopo Task 1. Comando base: da `backend/`, `./venv/Scripts/python -m pytest <path>::<test> -v`.
- Ogni Task termina con commit SOLO se l'utente ha autorizzato git (il checkout NON è un repo git: in tal caso saltare lo step di commit e annotare il completamento nel piano spuntando le checkbox).

---

## File Structure (cosa si tocca e perché)

| File | Responsabilità | Task |
|---|---|---|
| `.gitignore` (root, nuovo) | Impedire commit di segreti/artefatti | 7 |
| `backend/requirements.txt`, `backend/pyproject.toml` | Dipendenze runtime/test coerenti e riproducibili | 1 |
| `backend/app/browser/context_manager.py` | Fix getter ricorsivo `measureText` | 2 |
| `backend/app/main.py` | Rimuovere auto-pause campagne nel lifespan | 3 |
| `backend/app/services/campaign_orchestrator.py` | `account_id` salvato nella transizione a `sending` | 4 |
| `backend/app/services/recovery_checker.py` | Rilascio reservation su retry senza evidenza | 4 |
| `backend/app/config.py`, `backend/app/utils/auth_deps.py` | Rimozione legacy auth mode (JWT obbligatorio) | 5 |
| `backend/app/api/campaigns.py` | Persistenza config avanzate + preflight Redis `start_scrape` + guard approvazione | 6, 8, 10 |
| `backend/app/services/ai_personalizer.py` | Fallback senza placeholder residui | 11 |
| `backend/tests/...` (nuovi) | Test di regressione | vari |

---

## Task 1: Ambiente riproducibile (prerequisito per i test)

**Files:**
- Modify: `backend/requirements.txt`
- Modify: `backend/pyproject.toml`

- [ ] **Step 1: Riabilitare le dipendenze browser e aggiungere pytest in requirements**

In `backend/requirements.txt` sostituire le righe:

```
# Browser automation (Phase 4)
# patchright>=1.0.0
# humanization-playwright>=0.1.0
```

con:

```
# Browser automation (Phase 4) — runtime obbligatorie per invio DM
patchright>=1.0.0
humanization-playwright>=0.1.0
```

E nella sezione `# Dev` aggiungere (se mancante) `pytest-asyncio` già presente; aggiungere `anyio>=4.0.0` sotto `pytest-asyncio`.

- [ ] **Step 2: Installare le dev-deps nel venv**

Run (da `backend/`): `./venv/Scripts/python -m pip install pytest pytest-asyncio anyio`
Expected: `Successfully installed pytest...`

- [ ] **Step 3: Verificare pytest eseguibile**

Run (da `backend/`): `./venv/Scripts/python -m pytest --version`
Expected: stampa versione, exit 0.

- [ ] **Step 4: Creare la cartella test e il conftest**

Create `backend/tests/__init__.py` (vuoto).
Create `backend/tests/conftest.py`:

```python
import asyncio
import pytest


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
```

- [ ] **Step 5: Commit** (saltare se non-git)

```bash
git add backend/requirements.txt backend/pyproject.toml backend/tests/__init__.py backend/tests/conftest.py
git commit -m "chore: restore runtime browser deps and make pytest runnable"
```

---

## Task 2: C6 — Fix getter ricorsivo `measureText`

**Files:**
- Modify: `backend/app/browser/context_manager.py:348-357`

- [ ] **Step 1: Applicare il fix**

In `backend/app/browser/context_manager.py`, dentro `_build_fingerprint_script`, sostituire il blocco:

```
    const _origMeasureText = CanvasRenderingContext2D.prototype.measureText;
    CanvasRenderingContext2D.prototype.measureText = function(text) {{
        const metrics = _origMeasureText.call(this, text);
        const noise = (Math.random() - 0.5) * 0.02;
        Object.defineProperty(metrics, 'width', {{
            get: () => metrics.width + noise,
            configurable: true,
        }});
        return metrics;
    }};
```

con:

```
    const _origMeasureText = CanvasRenderingContext2D.prototype.measureText;
    CanvasRenderingContext2D.prototype.measureText = function(text) {{
        const metrics = _origMeasureText.call(this, text);
        const _origWidth = metrics.width;
        const noise = (Math.random() - 0.5) * 0.02;
        Object.defineProperty(metrics, 'width', {{
            get: () => _origWidth + noise,
            configurable: true,
        }});
        return metrics;
    }};
```

- [ ] **Step 2: Verifica sintassi Python**

Run (da `backend/`): `./venv/Scripts/python -m compileall app/browser/context_manager.py`
Expected: nessun `SyntaxError`.

- [ ] **Step 3: Verifica logica JS (no ricorsione)**

Run: `node -e "const m={width:42};const o=m.width;const n=0.01;Object.defineProperty(m,'width',{get:()=>o+n});console.log(m.width)"`
Expected: stampa `42.01` (nessun RangeError). Se `node` non disponibile, ispezione manuale: il getter usa `_origWidth`, non `metrics.width`.

- [ ] **Step 4: Commit** (saltare se non-git)

```bash
git add backend/app/browser/context_manager.py
git commit -m "fix(browser): avoid recursive measureText width getter (RangeError)"
```

---

## Task 3: C4 — Rimuovere auto-pause campagne dal lifespan API

**Files:**
- Modify: `backend/app/main.py`

- [ ] **Step 1: Rimuovere la chiamata nel lifespan**

In `backend/app/main.py`, nella funzione `lifespan`, eliminare la riga:

```
    await _auto_pause_orphaned_campaigns()
```

- [ ] **Step 2: Rimuovere la funzione ora inutilizzata**

Eliminare l'intera funzione `async def _auto_pause_orphaned_campaigns():` (dal `def` fino all'ultima riga del suo corpo, righe ~43-76).

- [ ] **Step 3: Rimuovere import non più usato**

In `backend/app/main.py` riga 9 `from sqlalchemy import update` → eliminarla (verificare prima che `update` non sia usato altrove nel file: `grep -n "update(" backend/app/main.py` deve dare 0 risultati dopo lo Step 2).

- [ ] **Step 4: Verifica sintassi**

Run (da `backend/`): `./venv/Scripts/python -m compileall app/main.py`
Expected: nessun `SyntaxError`.

- [ ] **Step 5: Commit** (saltare se non-git)

```bash
git add backend/app/main.py
git commit -m "fix(api): do not auto-pause campaigns on API start (ARQ worker is separate process)"
```

**Nota residua:** la crash-recovery resta garantita dal cron `release_stale_locks` in `task_queue.py` (auto-pause su inattività 30 min). Nessuna azione aggiuntiva.

---

## Task 4: C5 — `sending` atomico con `account_id` + rilascio reservation su retry

**Files:**
- Modify: `backend/app/services/campaign_orchestrator.py` (blocco "Mark status as 'sending'")
- Modify: `backend/app/services/recovery_checker.py` (`_recover_one`, ramo no-evidence retry)
- Test: `backend/tests/test_recovery_release.py`

- [ ] **Step 1: Salvare `account_id` nella stessa transizione a `sending`**

In `backend/app/services/campaign_orchestrator.py`, sostituire:

```python
                message.status = MessageStatus.sending
                message.updated_at = datetime.utcnow()
                await db.commit()
```

con:

```python
                message.status = MessageStatus.sending
                message.account_id = account_id
                message.updated_at = datetime.utcnow()
                await db.commit()
```

- [ ] **Step 2: Rilasciare la reservation quando il recovery rimette in retry senza evidenza**

In `backend/app/services/recovery_checker.py`, dentro `_recover_one`, nel ramo che resetta a retry (quello con `msg.status = MessageStatus.retry` e `follower.status = FollowerStatus.message_generated`), PRIMA della riga `await db.commit()` aggiungere:

```python
    from app.services.campaign_orchestrator import _release_global_contact_reservation
    await _release_global_contact_reservation(follower.ig_user_id, db)
```

(Il ramo `if msg.retry_count >= 1: ... return "skipped"` NON va modificato: lì il DM potrebbe essere stato consegnato e la reservation va mantenuta.)

- [ ] **Step 3: Scrivere il test di regressione (release su retry)**

Create `backend/tests/test_recovery_release.py`:

```python
import pytest
from app.services.campaign_orchestrator import _release_global_contact_reservation
from app.database import AsyncSessionLocal
from app.models.global_contact import GlobalContact
from sqlalchemy import select
import uuid
from datetime import datetime


@pytest.mark.asyncio
async def test_release_deletes_reservation_row():
    ig_id = 999999999000 + int(datetime.utcnow().timestamp()) % 100000
    async with AsyncSessionLocal() as db:
        db.add(GlobalContact(
            id=str(uuid.uuid4()), ig_user_id=ig_id,
            contacted_by_campaign_ids="[]", contact_history="[]",
            created_at=datetime.utcnow(),
        ))
        await db.commit()
        await _release_global_contact_reservation(ig_id, db)
        await db.commit()
        row = await db.scalar(select(GlobalContact).where(GlobalContact.ig_user_id == ig_id))
        assert row is None
```

- [ ] **Step 4: Eseguire il test (deve passare)**

Run (da `backend/`): `./venv/Scripts/python -m pytest tests/test_recovery_release.py -v`
Expected: 1 passed. (Richiede DB raggiungibile come da `.env`.)

- [ ] **Step 5: Verifica sintassi entrambi i file**

Run (da `backend/`): `./venv/Scripts/python -m compileall app/services/campaign_orchestrator.py app/services/recovery_checker.py`
Expected: nessun `SyntaxError`.

- [ ] **Step 6: Commit** (saltare se non-git)

```bash
git add backend/app/services/campaign_orchestrator.py backend/app/services/recovery_checker.py backend/tests/test_recovery_release.py
git commit -m "fix(dm): persist account_id with 'sending' and release reservation on recovery retry"
```

---

## Task 5: C2 — Rimuovere la legacy auth mode (JWT obbligatorio)

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/app/utils/auth_deps.py`

- [ ] **Step 1: Rendere `jwt_secret` obbligatorio in config**

In `backend/app/config.py`, sotto la riga `jwt_secret: str = ""` aggiungere un validator (dopo l'import esistente `from pydantic import field_validator`):

```python
    @field_validator("jwt_secret")
    @classmethod
    def validate_jwt_secret(cls, v: str) -> str:
        if not v or len(v) < 16:
            raise ValueError(
                "JWT_SECRET non impostato (o troppo corto) nel file .env. "
                'Genera con: python -c "import secrets; print(secrets.token_urlsafe(32))"'
            )
        return v
```

- [ ] **Step 2: Eliminare la legacy mode in auth_deps**

In `backend/app/utils/auth_deps.py` rimuovere:
- la funzione `def _auth_enabled() -> bool:` e il suo corpo;
- il blocco `_LEGACY_USER = User(...)`;
- nel corpo di `get_current_user`, le righe:

```python
    if not _auth_enabled():
        return _LEGACY_USER

```

Il resto di `get_current_user` (richiede token valido) resta invariato.

- [ ] **Step 3: Aggiornare il docstring del modulo**

Sostituire il docstring iniziale di `backend/app/utils/auth_deps.py` con:

```python
"""FastAPI dependencies for JWT auth.

Auth is ALWAYS enforced. `JWT_SECRET` is required at startup (validated in
config). Missing/invalid/expired token → 401. `require_admin` 403 for non-admin.
"""
```

- [ ] **Step 4: Verifica avvio fallisce senza JWT_SECRET**

Run (da `backend/`): `JWT_SECRET= ./venv/Scripts/python -c "from app.config import Settings; Settings()"` (Windows PowerShell: `$env:JWT_SECRET=''; ./venv/Scripts/python -c "from app.config import Settings; Settings()"`)
Expected: `ValidationError` con messaggio "JWT_SECRET non impostato".

- [ ] **Step 5: Verifica avvio OK con JWT_SECRET presente**

Run (da `backend/`): `./venv/Scripts/python -c "from app.config import settings; print(bool(settings.jwt_secret))"`
Expected: `True` (usa il `.env` reale).

- [ ] **Step 6: Verifica sintassi**

Run (da `backend/`): `./venv/Scripts/python -m compileall app/config.py app/utils/auth_deps.py`
Expected: nessun `SyntaxError`.

- [ ] **Step 7: Commit** (saltare se non-git)

```bash
git add backend/app/config.py backend/app/utils/auth_deps.py
git commit -m "security: remove legacy no-auth mode; JWT_SECRET now required"
```

**Nota:** il frontend (`AuthGuard.tsx`) già forza il login → nessuna modifica frontend necessaria. Bootstrap primo admin: `python -m scripts.create_admin --email <e> --password <p>`.

---

## Task 6: U7 — Persistere le config avanzate della campagna (create/update)

**Files:**
- Modify: `backend/app/api/campaigns.py` (`create_campaign`, `update_campaign`)
- Test: `backend/tests/test_campaign_config_persist.py`

Le colonne esistono già in `models/campaign.py` e nello schema Pydantic; mancano solo nel costruttore `Campaign(...)` e negli updater.

- [ ] **Step 1: Mappare i campi in `create_campaign`**

In `backend/app/api/campaigns.py`, nel costruttore `campaign = Campaign(...)` di `create_campaign`, aggiungere prima di `status=CampaignStatus.draft,`:

```python
        scrape_session_size=data.scrape_session_size,
        scrape_break_minutes_min=data.scrape_break_minutes_min,
        scrape_break_minutes_max=data.scrape_break_minutes_max,
        bio_fetch_delay_min=data.bio_fetch_delay_min,
        bio_fetch_delay_max=data.bio_fetch_delay_max,
```

- [ ] **Step 2: Mappare i campi in `update_campaign`**

In `update_campaign`, dopo il blocco `if data.scrape_mode is not None: campaign.scrape_mode = data.scrape_mode`, aggiungere:

```python
    if data.scrape_session_size is not None:
        campaign.scrape_session_size = data.scrape_session_size
    if data.scrape_break_minutes_min is not None:
        campaign.scrape_break_minutes_min = data.scrape_break_minutes_min
    if data.scrape_break_minutes_max is not None:
        campaign.scrape_break_minutes_max = data.scrape_break_minutes_max
    if data.bio_fetch_delay_min is not None:
        campaign.bio_fetch_delay_min = data.bio_fetch_delay_min
    if data.bio_fetch_delay_max is not None:
        campaign.bio_fetch_delay_max = data.bio_fetch_delay_max
```

- [ ] **Step 3: Validazione coerenza min<=max (create e update)**

In `create_campaign`, subito dopo aver costruito `campaign` e prima di `db.add(campaign)`, aggiungere:

```python
    if campaign.scrape_break_minutes_min > campaign.scrape_break_minutes_max:
        raise HTTPException(status_code=400, detail="scrape_break_minutes_min > max")
    if campaign.bio_fetch_delay_min > campaign.bio_fetch_delay_max:
        raise HTTPException(status_code=400, detail="bio_fetch_delay_min > max")
```

In `update_campaign`, prima di `campaign.updated_at = datetime.utcnow()`, aggiungere lo stesso doppio check (riusare le due righe `raise` identiche).

- [ ] **Step 4: Test di persistenza**

Create `backend/tests/test_campaign_config_persist.py`:

```python
import pytest
from app.schemas.campaign import CampaignCreate
from app.models.campaign import Campaign


def test_create_schema_carries_advanced_fields():
    c = CampaignCreate(
        name="t", target_username="x", base_message_template="0123456789",
        scrape_session_size=123, scrape_break_minutes_min=10,
        scrape_break_minutes_max=20, bio_fetch_delay_min=2.0, bio_fetch_delay_max=9.0,
    )
    assert c.scrape_session_size == 123
    assert c.bio_fetch_delay_max == 9.0


def test_model_accepts_advanced_fields():
    m = Campaign(
        name="t", target_username="x", base_message_template="0123456789",
        scrape_session_size=123, bio_fetch_delay_min=2.0, bio_fetch_delay_max=9.0,
    )
    assert m.scrape_session_size == 123
```

- [ ] **Step 5: Eseguire i test**

Run (da `backend/`): `./venv/Scripts/python -m pytest tests/test_campaign_config_persist.py -v`
Expected: 2 passed.

- [ ] **Step 6: Verifica sintassi**

Run (da `backend/`): `./venv/Scripts/python -m compileall app/api/campaigns.py`
Expected: nessun `SyntaxError`.

- [ ] **Step 7: Commit** (saltare se non-git)

```bash
git add backend/app/api/campaigns.py backend/tests/test_campaign_config_persist.py
git commit -m "fix(campaigns): persist advanced scrape/delay config from UI + validate min<=max"
```

---

## Task 7: C1 — Igiene segreti (.gitignore + procedura rotazione)

**Files:**
- Create: `.gitignore` (root)
- Create: `docs/setup/SECRET_ROTATION.md`

- [ ] **Step 1: Creare `.gitignore` root**

Create `.gitignore`:

```
# Secrets / env
.env
.env.*
!.env.example
.vercel/

# Backups (contengono copie .env)
backups/BOT_OUTBOUND_BACKUP/

# Runtime data
backend/data/
backend/venv/
**/__pycache__/
*.pyc

# Frontend
frontend/node_modules/
frontend/.next/
```

- [ ] **Step 2: Documentare la rotazione (azione manuale dell'utente)**

Create `docs/setup/SECRET_ROTATION.md`:

```markdown
# Rotazione segreti — OBBLIGATORIA (segreti esposti)

I segreti in `.env` e `backups/BOT_OUTBOUND_BACKUP/.env` sono stati esposti. Ruotare TUTTI:

1. **Supabase DB password**: dashboard Supabase → Database → Reset password. Aggiornare `DATABASE_URL` in `.env`.
2. **SECRET_KEY (Fernet)**: genera `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
   ⚠️ Cambiare questa chiave invalida le password account IG cifrate: re-inserire le password account dopo il cambio (o script di re-encrypt con vecchia+nuova chiave).
3. **JWT_SECRET**: `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Invalida i token attivi (re-login).
4. **TELEGRAM_BOT_TOKEN**: @BotFather → /revoke → nuovo token.
5. **AI_API_KEY (Groq)**: console.groq.com → revoca + nuova key.

Dopo la rotazione: eliminare `backups/BOT_OUTBOUND_BACKUP/` dal working dir (azione distruttiva — conferma esplicita).
```

- [ ] **Step 3: Verifica .gitignore copre i file sensibili**

Run (da root): `git check-ignore .env backups/BOT_OUTBOUND_BACKUP/.env 2>/dev/null || echo "not-a-git-repo"`
Expected: stampa i path ignorati, oppure `not-a-git-repo` (in tal caso il `.gitignore` sarà comunque attivo a `git init`).

- [ ] **Step 4: Commit** (saltare se non-git)

```bash
git add .gitignore docs/setup/SECRET_ROTATION.md
git commit -m "security: add root .gitignore and secret rotation procedure"
```

**Azione manuale richiesta all'utente (NON automatizzabile):** eseguire la rotazione in `docs/setup/SECRET_ROTATION.md` e confermare l'eliminazione di `backups/BOT_OUTBOUND_BACKUP/`.

---

## Task 8: U6 — Preflight Redis in `start_scrape`

**Files:**
- Modify: `backend/app/api/campaigns.py` (`start_scrape`)

- [ ] **Step 1: Aggiungere il preflight Redis prima della commit**

In `start_scrape`, dopo il check `if campaign.status not in (CampaignStatus.draft,): raise ...` e PRIMA di `campaign.status = CampaignStatus.scraping`, aggiungere:

```python
    if not await _check_redis_reachable():
        raise HTTPException(
            status_code=503,
            detail="Redis non raggiungibile. Avviare Redis prima dello scraping.",
        )
```

- [ ] **Step 2: Verifica sintassi**

Run (da `backend/`): `./venv/Scripts/python -m compileall app/api/campaigns.py`
Expected: nessun `SyntaxError`.

- [ ] **Step 3: Commit** (saltare se non-git)

```bash
git add backend/app/api/campaigns.py
git commit -m "fix(campaigns): preflight Redis before marking campaign 'scraping'"
```

---

## Task 9: U1 — Bloccare invio non approvato in auto-gen

**Files:**
- Modify: `backend/app/services/campaign_orchestrator.py` (`_get_or_create_message`)

- [ ] **Step 1: Aggiungere la guard di approvazione alla generazione inline**

In `_get_or_create_message`, subito dopo `db.add(message)` e prima di `follower.status = FollowerStatus.message_generated`, sostituire:

```python
        db.add(message)
        follower.status = FollowerStatus.message_generated
        await db.commit()
        await db.refresh(message)
        return message
```

con:

```python
        db.add(message)
        if campaign.require_approval:
            follower.status = FollowerStatus.pending_approval
            await db.commit()
            logger.info(
                f"[Worker] @{follower.username} require_approval attivo — "
                "messaggio in attesa di approvazione, non inviato"
            )
            return None
        follower.status = FollowerStatus.message_generated
        await db.commit()
        await db.refresh(message)
        return message
```

(Ritornare `None` fa sì che il worker rilasci il lock e la reservation e passi oltre, come già gestito a valle.)

- [ ] **Step 2: Verifica sintassi**

Run (da `backend/`): `./venv/Scripts/python -m compileall app/services/campaign_orchestrator.py`
Expected: nessun `SyntaxError`.

- [ ] **Step 3: Commit** (saltare se non-git)

```bash
git add backend/app/services/campaign_orchestrator.py
git commit -m "fix(dm): respect require_approval in auto-gen path (no unapproved sends)"
```

---

## Task 10: U9 — Fallback senza placeholder residui

**Files:**
- Modify: `backend/app/services/ai_personalizer.py` (`_fallback_message`, `_validate_message`)
- Test: `backend/tests/test_fallback_placeholder.py`

- [ ] **Step 1: Rendere il fallback safe**

In `backend/app/services/ai_personalizer.py` sostituire `_fallback_message`:

```python
def _fallback_message(base_template: str, name: str) -> str:
    return base_template.replace("{name}", name).replace("{nome}", name).replace("[Nome]", name).replace("[nome]", name)
```

con:

```python
import re as _re_fb

_RESIDUAL_PLACEHOLDER_RE = _re_fb.compile(r"[{\[][^{}\[\]]{0,40}[}\]]")


def _fallback_message(base_template: str, name: str) -> str:
    msg = (
        base_template
        .replace("{name}", name).replace("{nome}", name)
        .replace("[Nome]", name).replace("[nome]", name)
        .replace("{Name}", name).replace("[Name]", name)
    )
    # Se restano segnaposto non sostituiti, segnala impossibilità di fallback sicuro.
    if _RESIDUAL_PLACEHOLDER_RE.search(msg):
        raise OllamaError(
            f"Fallback non sicuro: placeholder residui nel template ({msg[:80]!r})"
        )
    return msg
```

- [ ] **Step 2: Test**

Create `backend/tests/test_fallback_placeholder.py`:

```python
import pytest
from app.services.ai_personalizer import _fallback_message
from app.utils.exceptions import OllamaError


def test_fallback_replaces_name():
    assert _fallback_message("Ciao {nome}!", "Mario") == "Ciao Mario!"


def test_fallback_raises_on_residual_placeholder():
    with pytest.raises(OllamaError):
        _fallback_message("Ciao {azienda}, offerta per [Nome2]", "Mario")
```

- [ ] **Step 3: Eseguire i test**

Run (da `backend/`): `./venv/Scripts/python -m pytest tests/test_fallback_placeholder.py -v`
Expected: 2 passed.

Nota a valle: `generate_message` propaga `OllamaError`; il chiamante `_get_or_create_message` lo cattura e marca il follower `failed` (comportamento corretto: meglio fallire che inviare un placeholder). Vedi M6 in Fase 1 per rendere transitorio invece di `failed`.

- [ ] **Step 4: Verifica sintassi**

Run (da `backend/`): `./venv/Scripts/python -m compileall app/services/ai_personalizer.py`
Expected: nessun `SyntaxError`.

- [ ] **Step 5: Commit** (saltare se non-git)

```bash
git add backend/app/services/ai_personalizer.py backend/tests/test_fallback_placeholder.py
git commit -m "fix(ai): never send messages with residual unfilled placeholders"
```

---

## Self-Review

- **Spec coverage:** Fase 0 di `AUDIT_UNIFICATO.md` = C1(T7), C2(T5), C5(T2/C6 → T2, C5 → T4), C4(T3), U6(T8), U7(T6), U1(T9), U9(T10), U10(T1). U8 (export CSV) rinviato a Fase 1 (richiede modifica frontend `leads/page.tsx`, non urgente quanto i precedenti). Annotato come gap volontario.
- **Placeholder scan:** nessun "TBD"; ogni step ha codice/comando concreto.
- **Type consistency:** `_release_global_contact_reservation(ig_user_id, db)` usato in T4 coerente con la firma in `campaign_orchestrator.py`. `OllamaError` (T10) è l'eccezione realmente sollevata da `generate_message`. `_check_redis_reachable` (T8) è funzione esistente in `campaigns.py`.

---

## Execution Handoff

Esecuzione: i Task **2, 3, 4, 5, 6, 7(parziale), 8, 9, 10** sono modifiche codice sicure ed eseguibili subito. Task 1 richiede `pip install` (rete) e Task 7 una azione manuale dell'utente (rotazione segreti + eliminazione backup). Il checkout non è git → gli step di commit vanno saltati e tracciati spuntando le checkbox.
