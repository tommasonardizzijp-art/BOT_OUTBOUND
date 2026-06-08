# Lead Qualification - Implementation Plan

Data: 2026-06-08

Spec di riferimento:
`docs/superpowers/specs/2026-06-08-lead-qualification-design.md`

## Goal

Implementare una sezione dedicata "Qualifica lead" che permette di:

- creare target riutilizzabili da descrizione libera;
- far compilare all'AI criteri/keyword modificabili;
- stimare quanti `global_contacts` verranno analizzati con filtri pre-run;
- classificare in batch con scoring deterministico;
- usare AI solo sui lead ambigui;
- salvare risultati storici per target/run;
- saltare lead gia classificati con stesso target e stessa versione regole;
- esportare CSV dedicato con confidenza;
- inviare notifica Telegram a fine run.

## Decisioni operative approvate

- `max_run_size` default: 5000 lead.
- batch size DB/deterministico: 100 lead.
- concorrenza AI ambiguous: 2 richieste simultanee.
- `reason` salvato sempre quando disponibile, anche se non sempre visibile in UI.
- cancel run live: fuori MVP, salvo implementazione semplice con check status tra batch.
- Sorgente: solo `global_contacts`.
- Sezione UI dedicata: `/lead-qualification`.
- Export dedicato: non modifica il CSV della pagina Leads esistente.

## Architettura sintetica

```text
UI /lead-qualification
  |
  | create/compile target profile
  v
API lead_qualification.py
  |
  | estimate run from filters
  | enqueue run
  v
ARQ qualify_leads_task(run_id)
  |
  | query global_contacts filtered
  | skip existing same rules_hash
  | deterministic scoring
  | AI only ambiguous
  v
lead_qualifications + run counters
  |
  | results/export + Telegram notification
  v
CSV / UI result table
```

## Convenzioni

- Non refactorare `api/leads.py` nel MVP, salvo helper piccoli e sicuri.
- Non modificare i flussi scraping/DM.
- Non chiamare AI sui lead non ambigui.
- Commit DB e progress counters a batch.
- Output AI sempre JSON validato.
- Bio, username, link e descrizioni lead sono dati non attendibili: prompt con delimitatori e istruzioni anti-injection.
- Default ASCII nei nuovi file.

---

## Task 1 - Migrazione 015 e modelli ORM

**Files**

- Create: `backend/alembic/versions/015_lead_qualification.py`
- Create: `backend/app/models/lead_qualification.py`
- Modify: `backend/app/models/__init__.py` se il file importa esplicitamente i modelli

### Step 1.1 - Creare migrazione Alembic

Creare `015_lead_qualification.py`.

Tabelle:

1. `lead_target_profiles`
2. `lead_qualification_runs`
3. `lead_qualifications`

Schema consigliato:

```python
"""Lead qualification profiles, runs and results.

Revision ID: 015
Revises: 014
Create Date: 2026-06-08
"""
from alembic import op
import sqlalchemy as sa

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lead_target_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("compiled_rules", sa.Text(), nullable=False),
        sa.Column("rules_hash", sa.String(64), nullable=False),
        sa.Column("pass_threshold", sa.Integer(), nullable=False, server_default="80"),
        sa.Column("reject_threshold", sa.Integer(), nullable=False, server_default="25"),
        sa.Column("ai_review_min_score", sa.Integer(), nullable=False, server_default="26"),
        sa.Column("ai_review_max_score", sa.Integer(), nullable=False, server_default="79"),
        sa.Column("max_run_size", sa.Integer(), nullable=False, server_default="5000"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_lead_target_profiles_created_at", "lead_target_profiles", ["created_at"])

    op.create_table(
        "lead_qualification_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("target_profile_id", sa.String(36), sa.ForeignKey("lead_target_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filters", sa.Text(), nullable=False),
        sa.Column("rules_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("total_candidates", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_existing", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("matched_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("no_match_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ambiguous_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ai_reviewed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_lq_runs_profile_status", "lead_qualification_runs", ["target_profile_id", "status"])
    op.create_index("ix_lq_runs_created_at", "lead_qualification_runs", ["created_at"])

    op.create_table(
        "lead_qualifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("global_contact_id", sa.String(36), sa.ForeignKey("global_contacts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ig_user_id", sa.BigInteger(), nullable=False),
        sa.Column("target_profile_id", sa.String(36), sa.ForeignKey("lead_target_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("lead_qualification_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rules_hash", sa.String(64), nullable=False),
        sa.Column("deterministic_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ai_score", sa.Integer(), nullable=True),
        sa.Column("final_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("matched_signals", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("negative_signals", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("ai_used", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ai_label", sa.String(255), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("model_used", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_lq_target_contact_rules", "lead_qualifications", ["target_profile_id", "global_contact_id", "rules_hash"])
    op.create_index("ix_lq_target_status_score", "lead_qualifications", ["target_profile_id", "status", "final_score"])
    op.create_index("ix_lq_run", "lead_qualifications", ["run_id"])
    op.create_index("ix_lq_ig_user_id", "lead_qualifications", ["ig_user_id"])


def downgrade() -> None:
    op.drop_index("ix_lq_ig_user_id", table_name="lead_qualifications")
    op.drop_index("ix_lq_run", table_name="lead_qualifications")
    op.drop_index("ix_lq_target_status_score", table_name="lead_qualifications")
    op.drop_index("ix_lq_target_contact_rules", table_name="lead_qualifications")
    op.drop_table("lead_qualifications")
    op.drop_index("ix_lq_runs_created_at", table_name="lead_qualification_runs")
    op.drop_index("ix_lq_runs_profile_status", table_name="lead_qualification_runs")
    op.drop_table("lead_qualification_runs")
    op.drop_index("ix_lead_target_profiles_created_at", table_name="lead_target_profiles")
    op.drop_table("lead_target_profiles")
```

Nota: usare `datetime.utcnow` nei modelli per `created_at`; nella migrazione
non serve `server_default=func.now()` per coerenza col resto del repo.

### Step 1.2 - Creare modelli ORM

Creare `backend/app/models/lead_qualification.py`.

Classi:

- `LeadQualificationRunStatus`
- `LeadQualificationStatus`
- `LeadTargetProfile`
- `LeadQualificationRun`
- `LeadQualification`

Pattern:

```python
import enum
import uuid
from datetime import datetime
from sqlalchemy import String, Integer, Boolean, DateTime, Text, BigInteger, ForeignKey, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class LeadQualificationRunStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class LeadQualificationStatus(str, enum.Enum):
    match = "match"
    no_match = "no_match"
    ambiguous = "ambiguous"
    error = "error"
```

Usare `SAEnum(..., native_enum=False)` come in `Follower`.

### Step 1.3 - Verificare import

Comando:

```bash
cd backend
venv\Scripts\python.exe -c "from app.models.lead_qualification import LeadTargetProfile, LeadQualificationRun, LeadQualification; print('lead qualification models ok')"
```

Expected: import OK.

---

## Task 2 - Schemi Pydantic e utility regole

**Files**

- Create: `backend/app/schemas/lead_qualification.py`
- Create: `backend/app/services/lead_qualification_rules.py`
- Create: `backend/tests/test_lead_qualification_rules.py`

### Step 2.1 - Schemi Pydantic

Creare schemi per:

- `LeadTargetProfileCreate`
- `LeadTargetProfileUpdate`
- `LeadTargetProfileResponse`
- `CompileProfileRequest`
- `CompileProfileResponse`
- `LeadQualificationFilters`
- `LeadQualificationEstimateRequest`
- `LeadQualificationEstimateResponse`
- `LeadQualificationRunCreate`
- `LeadQualificationRunResponse`
- `LeadQualificationResultResponse`
- `LeadQualificationListResponse`

Campi filtro:

```python
class LeadQualificationFilters(BaseModel):
    date_from: str | None = None
    date_to: str | None = None
    campaign_ids: list[str] = []
    scraping_account_ids: list[str] = []
    has_phone: bool = False
    has_email: bool = False
    min_followers: int | None = None
    max_leads: int = 5000
    skip_existing_same_rules: bool = True
```

Validazioni:

- `max_leads`: `ge=1`, `le=5000` per MVP.
- soglie: 0-100.
- `ai_review_min_score <= ai_review_max_score`.
- `reject_threshold < pass_threshold`.

### Step 2.2 - Utility rules/hash

Creare `lead_qualification_rules.py`.

Funzioni:

- `normalize_compiled_rules(rules: dict) -> dict`
- `rules_hash(rules: dict) -> str`
- `safe_json_loads(raw: str | None, default)`
- `safe_json_dumps(value) -> str`
- `validate_compiled_rules(rules: dict) -> dict`

`rules_hash`:

- normalizza liste stringhe con strip/lower;
- ordina liste dove l'ordine non conta;
- serializza con `sort_keys=True`;
- sha256 hex.

### Step 2.3 - Test utility

Test minimi:

- hash stabile se cambia ordine keyword;
- hash cambia se cambia una keyword;
- default mancanti riempiti;
- soglie fuori range corrette/rifiutate;
- JSON non valido torna default dove previsto.

Comando:

```bash
cd backend
venv\Scripts\python.exe -m pytest tests/test_lead_qualification_rules.py -v
```

---

## Task 3 - Scoring deterministico

**Files**

- Create/Modify: `backend/app/services/lead_qualification.py`
- Create: `backend/tests/test_lead_qualification_scoring.py`

### Step 3.1 - Dataclass risultato

Nel servizio:

```python
from dataclasses import dataclass, field


@dataclass
class Signal:
    field: str
    term: str
    weight: int
    kind: str


@dataclass
class DeterministicScoreResult:
    score: int
    status: str
    matched_signals: list[dict] = field(default_factory=list)
    negative_signals: list[dict] = field(default_factory=list)
```

### Step 3.2 - Normalizzazione testo lead

Funzioni:

- `_normalize_text(value: str | None) -> str`
- `_term_in_text(term: str, text: str) -> bool`
- `_lead_fields(contact) -> dict[str, str]`

Campi:

- `username`
- `full_name`
- `biography`
- `external_url`
- `bio_links` concatenati da JSON
- `contact_fields` come marker se phone/email/whatsapp presenti
- `scrape_source` da JSON `scrape_sources`, se utile

### Step 3.3 - Calcolo score

Funzione:

```python
def score_lead(contact, compiled_rules: dict, thresholds: dict) -> DeterministicScoreResult:
    ...
```

Regole MVP:

- positive terms: bonus per match su qualsiasi campo, pesato per campo.
- strong terms: bonus maggiore.
- negative terms: penalty.
- external_url presente: piccolo bonus se target retail/ecommerce o se URL contiene keyword.
- contact disponibile: piccolo bonus, non decisivo.
- score clamp 0-100.

Soglie:

- `score >= pass_threshold`: `match`.
- `score <= reject_threshold`: `no_match`.
- altrimenti `ambiguous`.

### Step 3.4 - Test scoring

Test:

- boutique/abbigliamento -> match alto.
- influencer/fashion blogger -> no_match per negative terms.
- bio vaga con qualche keyword -> ambiguous.
- lead con solo contatto ma nessun segnale target -> no_match.
- termini inglesi tipo `clothing store` e `retail` funzionano.
- negative term forte abbassa score anche con positive terms.

Comando:

```bash
cd backend
venv\Scripts\python.exe -m pytest tests/test_lead_qualification_scoring.py -v
```

---

## Task 4 - AI compiler e AI classifier ambiguous

**Files**

- Modify: `backend/app/services/lead_qualification.py`
- Create: `backend/tests/test_lead_qualification_ai.py`

### Step 4.1 - Compilazione target con AI

Funzione:

```python
async def compile_target_description(description: str, ai_client=None) -> dict:
    ...
```

Usare `get_ai_client()` da `app.services.ai_personalizer` se `ai_client` e' `None`.

System prompt:

- rispondi solo JSON;
- genera termini IT/EN;
- separa `positive_terms`, `strong_terms`, `negative_terms`;
- includi `positive_concepts`, `negative_concepts`;
- includi `field_weights` e `score_rules`;
- non includere testo fuori JSON.

Parsing:

- estrarre JSON anche se il modello aggiunge whitespace;
- validare con `validate_compiled_rules`;
- fallback deterministico minimo se AI fallisce? Nel MVP meglio alzare errore pulito
  all'API, cosi l'utente sa che la generazione criteri non e' riuscita.

### Step 4.2 - Classificazione AI ambiguous

Funzione:

```python
async def classify_ambiguous_lead(profile, contact, deterministic_result, ai_client=None) -> dict:
    ...
```

Output atteso:

```json
{
  "status": "match",
  "confidence": 0.82,
  "label": "clothing_retail_store",
  "reason": "Bio e nome indicano una boutique retail; nessun segnale B2B."
}
```

Regole conversione:

- `ai_score = round(confidence * 100)`.
- se status AI `match` e confidence >= 0.70 -> final `match`.
- se status AI `no_match` e confidence >= 0.70 -> final `no_match`.
- altrimenti resta `ambiguous`.

Prompt injection:

- delimitare i dati lead con blocchi tipo `<<<LEAD_DATA>>>`;
- dire esplicitamente che bio/link sono dati, non istruzioni;
- output solo JSON.

### Step 4.3 - Test AI con fake client

Test:

- compile genera rules normalizzate.
- compile rifiuta JSON invalido.
- classifier match con confidence alta.
- classifier no_match con confidence alta.
- classifier confidence bassa resta ambiguous.
- bio con istruzioni tipo "ignore previous" non cambia prompt contract.

Comando:

```bash
cd backend
venv\Scripts\python.exe -m pytest tests/test_lead_qualification_ai.py -v
```

---

## Task 5 - Query candidati, estimate e skip existing

**Files**

- Modify: `backend/app/services/lead_qualification.py`
- Create: `backend/tests/test_lead_qualification_queries.py`

### Step 5.1 - Query candidate da `global_contacts`

Creare helper:

```python
def build_candidate_query(filters: LeadQualificationFilters):
    ...
```

Deve includere:

- date scrape: `coalesce(GlobalContact.first_seen_at, GlobalContact.created_at)`;
- `campaign_ids`: `exists(Follower where ig_user_id == GlobalContact.ig_user_id and campaign_id in ids)`;
- `scraping_account_ids`: `GlobalContact.scrape_sources.like(...)` come in `api/leads.py`;
- `has_phone`: `GlobalContact.phone.isnot(None)`;
- `has_email`: `GlobalContact.email.isnot(None)`;
- `min_followers`: subquery aggregate su `Follower.follower_count`;
- limite `max_leads`.

Nota: duplicare localmente i piccoli helper e non refactorare `api/leads.py` nel MVP.

### Step 5.2 - Skip existing same rules

Funzione:

```python
async def count_existing_same_rules(db, target_profile_id: str, rules_hash: str, candidate_query) -> int:
    ...
```

Il criterio di skip:

- stesso `target_profile_id`;
- stesso `global_contact_id`;
- stesso `rules_hash`;
- qualunque run precedente completata o risultato esistente non `error`.

### Step 5.3 - Estimate

Funzione:

```python
async def estimate_run(db, target_profile, filters) -> dict:
    ...
```

Output:

- `candidate_count`;
- `already_qualified_same_rules`;
- `will_process`;
- `over_limit`;
- `max_run_size`.

Regola:

- se `will_process > min(filters.max_leads, target_profile.max_run_size)`, API rifiuta start.

### Step 5.4 - Test query

Test con SQLite o query compile:

- date range usa `first_seen_at` fallback `created_at`;
- campaign_ids usa exists;
- phone/email filter;
- skip same rules;
- over_limit true.

Comando:

```bash
cd backend
venv\Scripts\python.exe -m pytest tests/test_lead_qualification_queries.py -v
```

---

## Task 6 - API router

**Files**

- Create: `backend/app/api/lead_qualification.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/services/work_enqueue.py`
- Create: `backend/tests/test_lead_qualification_api.py`

### Step 6.1 - Router

Router:

```python
router = APIRouter(prefix="/lead-qualification", tags=["lead-qualification"])
```

Endpoints:

- `GET /profiles`
- `POST /profiles/compile`
- `POST /profiles`
- `GET /profiles/{profile_id}`
- `PUT /profiles/{profile_id}`
- `DELETE /profiles/{profile_id}`
- `POST /runs/estimate`
- `POST /runs`
- `GET /runs`
- `GET /runs/{run_id}`
- `GET /results`
- `GET /results/export`

### Step 6.2 - Profile endpoints

Create profile:

- validate compiled rules;
- compute `rules_hash`;
- default thresholds se mancanti.

Update profile:

- se `compiled_rules` cambia, ricalcolare `rules_hash`;
- aggiornare `updated_at`;
- non cancellare run storiche.

Delete profile:

- consentito se non ci sono run running/queued;
- cascade cancella runs/results storici, oppure rifiutare delete con run storiche?
- MVP consigliato: rifiutare se esistono run; meglio evitare perdita dati.

### Step 6.3 - Runs estimate/start

`POST /runs/estimate`:

Input:

```json
{
  "target_profile_id": "...",
  "filters": {
    "date_from": "2026-06-01",
    "date_to": "2026-06-08",
    "campaign_ids": [],
    "scraping_account_ids": [],
    "has_phone": false,
    "has_email": false,
    "min_followers": 100,
    "max_leads": 5000,
    "skip_existing_same_rules": true
  }
}
```

`POST /runs`:

- esegue estimate;
- rifiuta se `will_process == 0`;
- rifiuta se `over_limit`;
- crea run `queued`;
- enqueue `qualify_leads_task(run_id)`;
- ritorna run.

### Step 6.4 - Results/export

Results:

- join `LeadQualification`, `GlobalContact`, `LeadTargetProfile`, opzionale run;
- filtri `target_profile_id`, `run_id`, `status`, `min_score`;
- paginazione.

Export:

- target obbligatorio;
- default `status=match`, `min_score=80`;
- CSV dedicato.

Fieldnames:

```text
ig_user_id,username,full_name,biography,phone,email,whatsapp,external_url,
bio_links,target_profile,qualification_status,confidence_score,
deterministic_score,ai_score,ai_used,matched_signals,negative_signals,
first_seen_at,scrape_sources,scraping_accounts
```

### Step 6.5 - Registrare router

In `main.py`:

```python
from app.api import ..., lead_qualification
app.include_router(lead_qualification.router, prefix="/api", dependencies=_protected)
```

### Step 6.6 - Test API

Test:

- compile endpoint con monkeypatch fake AI.
- create/update/list profile.
- estimate rifiuta over limit.
- start run crea run queued e chiama enqueue mock.
- results/export produce CSV.

Comando:

```bash
cd backend
venv\Scripts\python.exe -m pytest tests/test_lead_qualification_api.py -v
```

---

## Task 7 - ARQ worker batch

**Files**

- Create: `backend/app/workers/lead_qualification_worker.py`
- Modify: `backend/app/workers/task_queue.py`
- Modify: `backend/app/services/work_enqueue.py`
- Create: `backend/tests/test_lead_qualification_worker.py`

### Step 7.1 - Enqueue helper

In `work_enqueue.py`:

```python
async def enqueue_lead_qualification(run_id: str) -> bool:
    ...
```

Job id:

```text
lead-qualification:{run_id}
```

Queue:

- `ARQ_MAIN_QUEUE`.

### Step 7.2 - Worker task

Creare:

```python
async def qualify_leads_task(ctx: dict, run_id: str) -> None:
    ...
```

Flow:

1. Load run + target profile.
2. Set run `running`, `started_at`.
3. Query candidati con filtri.
4. Skip existing same rules se richiesto.
5. Process batch da 100.
6. Per ogni lead:
   - deterministic scoring;
   - se `ambiguous`, chiamare AI classifier con semaforo 2;
   - salvare `LeadQualification`;
   - aggiornare counters in memoria.
7. Commit batch.
8. Aggiornare run counters.
9. A fine: `completed`, `completed_at`.
10. Telegram summary.
11. In errore: `failed`, `completed_at`, Telegram warning.

### Step 7.3 - AI concurrency

Per mantenere semplice:

- processare il batch deterministico;
- raccogliere gli ambiguous;
- eseguire AI su ambiguous con `asyncio.Semaphore(2)`;
- salvare risultati.

Se AI fallisce per un lead:

- non fallire tutta la run;
- salvare qualification `status=ambiguous`, `ai_used=True`, `reason="AI classification failed: ..."` o `status=error`?
- MVP consigliato: `status=ambiguous`, `error_count += 1`, per non perdere lead.

### Step 7.4 - Telegram

Usare `send_telegram` da `app.services.notifier`.

Messaggio finale:

```text
Qualifica lead completata
Target: Boutique abbigliamento retail
Processati: 3380
Match: 912
No match: 2180
Ambigui: 288
AI reviewed: 740
Errori: 3
```

### Step 7.5 - Registrare task in WorkerSettings

In `task_queue.py`:

```python
from app.workers.lead_qualification_worker import qualify_leads_task

class WorkerSettings:
    functions = [
        ...,
        qualify_leads_task,
    ]
```

Valutare `job_timeout`:

- attuale `3600`;
- se una run da 5000 con AI puo superare 1h, il piano operativo finale puo alzarlo a 7200 o rendere il task short-lived.
- MVP consigliato: alzare a `7200` solo se i test/uso reale lo richiedono; altrimenti lasciare 3600.

### Step 7.6 - Test worker

Test:

- run passa queued -> running -> completed.
- counts corretti.
- skip existing same rules.
- AI chiamata solo per ambiguous.
- AI failure non rompe tutta la run.
- Telegram mock chiamato a fine run.

Comando:

```bash
cd backend
venv\Scripts\python.exe -m pytest tests/test_lead_qualification_worker.py -v
```

---

## Task 8 - Frontend types e API wrapper

**Files**

- Modify: `frontend/lib/types.ts`
- Modify: `frontend/lib/api.ts`

### Step 8.1 - Tipi TS

Aggiungere:

- `LeadTargetProfile`
- `LeadTargetProfileCreate`
- `CompiledLeadRules`
- `LeadQualificationFilters`
- `LeadQualificationEstimate`
- `LeadQualificationRun`
- `LeadQualificationResult`
- `LeadQualificationResultList`

Status:

```typescript
export type LeadQualificationRunStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
export type LeadQualificationStatus = 'match' | 'no_match' | 'ambiguous' | 'error'
```

### Step 8.2 - API namespace

In `api.ts` aggiungere:

```typescript
leadQualification: {
  profiles: {
    list()
    compile(description)
    create(data)
    get(id)
    update(id, data)
    delete(id)
  },
  runs: {
    estimate(data)
    create(data)
    list()
    get(id)
  },
  results: {
    list(params)
    exportBlob(params)
  }
}
```

Query array:

- riusare `appendArray`.

### Step 8.3 - Typecheck

Comando:

```bash
cd frontend
npx tsc --noEmit
```

---

## Task 9 - Frontend pagina dedicata

**Files**

- Create: `frontend/app/lead-qualification/page.tsx`
- Modify: `frontend/components/layout/Sidebar.tsx`

### Step 9.1 - Sidebar

Aggiungere voce:

- label: `Qualifica lead`
- route: `/lead-qualification`
- icona lucide: `Filter`, `BadgeCheck`, `Target` o `Sparkles`.

### Step 9.2 - Layout pagina

Pagina divisa in blocchi operativi:

1. Header con titolo e descrizione breve.
2. Target profiles salvati.
3. Editor nuovo target.
4. Anteprima/modifica criteri.
5. Filtri run.
6. Stima e avvio.
7. Run recenti.
8. Risultati/export.

Non usare landing page. Prima schermata deve essere lo strumento utilizzabile.

### Step 9.3 - Target editor

Box descrizione libera con placeholder guidato:

```text
Descrivi il lead ideale. Specifica cosa includere, cosa escludere,
se cerchi retail/B2B/ecommerce/local business, lingua o mercato.
Esempio: negozi di abbigliamento retail, boutique e showroom; escludi
grossisti, influencer, fashion blogger e brand solo editoriali.
```

Bottone:

- `Genera criteri`

Output:

- nome suggerito;
- positive terms;
- strong terms;
- negative terms;
- thresholds;
- salva target.

Permettere modifica manuale:

- MVP semplice: textarea JSON per `compiled_rules`;
- UX migliore: campi tag-like per positive/strong/negative.
- Scelta MVP consigliata: campi semplici textarea comma-separated per le tre liste principali + JSON collapsible avanzato.

### Step 9.4 - Filtri run

Riusare pattern pagina Leads:

- date preset o input date;
- multi-select campagne;
- multi-select account scraping;
- checkbox telefono/email;
- min followers;
- max leads.

Bottone:

- `Stima`

Mostrare:

- candidati;
- gia qualificati;
- da processare;
- limite.

Bottone:

- `Avvia classificazione`, disabilitato se over limit o will_process zero.

### Step 9.5 - Run recenti

Tabella/card:

- target;
- status;
- progress;
- processed/total;
- match/no_match/ambiguous;
- AI reviewed;
- created/completed.

Polling:

- SWR refresh 5s se esistono run queued/running;
- 30s altrimenti.

### Step 9.6 - Risultati/export

Filtri:

- target profile;
- run opzionale;
- status, default `match`;
- min score, default 80.

Tabella:

- username;
- full_name;
- contatti;
- confidence;
- status;
- AI used;
- first_seen_at.

Export:

- `Esporta CSV`
- scarica CSV dedicato.

### Step 9.7 - Typecheck

Comando:

```bash
cd frontend
npx tsc --noEmit
```

---

## Task 10 - Integrazione export e risultati con filtri

**Files**

- Modify: `backend/app/api/lead_qualification.py`
- Create/Modify: `backend/tests/test_lead_qualification_export.py`

### Step 10.1 - Latest result per target+lead

Per results generali senza `run_id`, mostrare il risultato piu recente.

Strategia:

- subquery con `max(created_at)` per `(target_profile_id, global_contact_id)`;
- join su `LeadQualification`.

Per MVP, se SQLAlchemy diventa troppo complesso:

- filtrare per `run_id` quando selezionato;
- senza `run_id`, ordinare per `created_at desc` e accettare duplicati? No, meglio evitare duplicati.
- Piano consigliato: implementare subquery latest.

### Step 10.2 - Export default

Se non specificato:

- `status=match`;
- `min_score=80`.

`target_profile_id` obbligatorio.

### Step 10.3 - Test export

Test:

- export include confidence.
- export non include no_match con default.
- min_score applicato.
- target A e target B indipendenti.
- latest result non duplica lead.

---

## Task 11 - Documentazione e handoff migrazione

**Files**

- Modify: `docs/project/PROGRESS.md`
- Modify: `INDEX.md`
- Modify: `CLAUDE.md` se contiene mappa feature/DB

### Step 11.1 - Aggiornare PROGRESS

Aggiungere sezione:

```text
## 2026-06-08 - Lead Qualification design/piano
```

Se implementato:

- tabelle 015;
- pagina dedicata;
- AI compiler;
- scoring deterministico;
- AI ambiguous;
- CSV dedicato;
- Telegram completion.

### Step 11.2 - Aggiornare INDEX/CLAUDE

Documentare:

- nuova pagina `/lead-qualification`;
- nuova migrazione `015`;
- nuovo worker task;
- avvio worker invariato ma ora include task qualifica.

### Step 11.3 - Handoff migrazione

Migrazione da applicare a bot fermo:

```bash
cd backend
venv\Scripts\python.exe -m scripts.migrate
```

Verifica:

```bash
cd backend
venv\Scripts\python.exe -c "from app.models.lead_qualification import LeadTargetProfile; print('ok')"
```

---

## Task 12 - Verifica finale

### Backend unit/API

Comandi:

```bash
cd backend
venv\Scripts\python.exe -m pytest tests/test_lead_qualification_rules.py -v
venv\Scripts\python.exe -m pytest tests/test_lead_qualification_scoring.py -v
venv\Scripts\python.exe -m pytest tests/test_lead_qualification_ai.py -v
venv\Scripts\python.exe -m pytest tests/test_lead_qualification_queries.py -v
venv\Scripts\python.exe -m pytest tests/test_lead_qualification_api.py -v
venv\Scripts\python.exe -m pytest tests/test_lead_qualification_worker.py -v
venv\Scripts\python.exe -m pytest tests/test_lead_qualification_export.py -v
```

### Backend smoke

```bash
cd backend
venv\Scripts\python.exe -c "from app.main import app; print('app import ok')"
venv\Scripts\python.exe -m compileall app
```

### Frontend

```bash
cd frontend
npx tsc --noEmit
```

### Suite completa

```bash
cd backend
venv\Scripts\python.exe -m pytest tests -v
```

Nota: se test esistenti richiedono DB operativo/Postgres e falliscono per
ambiente, segnalarlo separatamente come preesistente.

---

## Sequenza consigliata di implementazione

1. DB + modelli.
2. Schemi + rules utility.
3. Scoring deterministico.
4. AI compile/classifier.
5. Query/estimate.
6. API + enqueue.
7. Worker.
8. Frontend types/API.
9. Frontend pagina.
10. Export/results latest.
11. Docs.
12. Verifica completa.

---

## Open points prima dell'implementazione

Questi punti sono gia impostati nel piano con proposta operativa. Cambiarli ora
evita refactor dopo:

1. Delete profile: proposta rifiutare delete se esistono run storiche.
2. AI failure per singolo lead: proposta lasciare `ambiguous`, incrementare `error_count`.
3. Editor criteri MVP: proposta campi semplici per liste principali + JSON avanzato.
4. `job_timeout`: proposta lasciare 3600 inizialmente, alzare solo se necessario.
5. Cancel run: fuori MVP.

Se approvati, partire dall'implementazione Task 1.

