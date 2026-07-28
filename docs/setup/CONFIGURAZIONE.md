# CONFIGURAZIONE E AVVIO LOCALE

Variabili `.env`, migrazioni, comandi di avvio dei 5 processi. Estratto da `CLAUDE.md` il 2026-07-29 (contenuto invariato).

---

## Configurazione (.env)

Il file `.env` va messo nella **root del progetto** (`d:\BOT OUTBOUND\.env`).
Alembic e FastAPI lo leggono tramite Pydantic Settings con `env_file=".env"`.

Variabili chiave:
- `SECRET_KEY`: chiave Fernet generata con `from cryptography.fernet import Fernet; Fernet.generate_key()`
- `DATABASE_URL`: **in produzione punta a Supabase Postgres** (`postgresql+asyncpg://...@...pooler.supabase.com...`). Il codice aggiunge automaticamente parametri safe per Supabase Pooler/PgBouncer (`prepared_statement_cache_size=0`, `statement_cache_size=0`, unique prepared statement names, `NullPool`) per evitare `DuplicatePreparedStatementError`.
  - Fallback dev locale: `sqlite+aiosqlite:///./data/bot.db` (relativo a `backend/`). Il codice mantiene i branch SQLite (vedi `app/utils/db_dialect.py`), ma il deployment reale è su Supabase.
  - **Le migrazioni Alembic girano contro Supabase** (`python -m scripts.migrate`). Attenzione: una connessione `idle in transaction` lasciata aperta da un processo bot morto tiene un lock su `campaigns`/`followers` e fa andare in timeout gli `ALTER TABLE` — fermare il bot e/o terminare il backend zombie prima di migrare.
  - Su Windows Python 3.13 puo' bloccarsi in WMI durante `platform.uname()`/`platform.machine()`, chiamato indirettamente da SQLAlchemy/asyncpg. Per questo `backend/app/database.py`, `backend/alembic/env.py` e `backend/scripts/migrate.py` patchano quelle funzioni prima degli import SQLAlchemy. Non rimuovere senza verificare migrazioni e import runtime.
- `OLLAMA_MODEL`: nome modello Ollama (usato solo se `AI_PROVIDER=ollama`)
- `AI_PROVIDER`: `ollama` | `groq` | `gemini` — seleziona provider LLM
- `AI_API_KEY`: API key del provider cloud (Groq: `gsk_...`, Gemini: `AIza...`)
- `AI_MODEL`: modello specifico (vuoto = default provider: Groq→`llama-3.3-70b-versatile`, Gemini→`gemini-2.5-flash`). ⚠️ Gemini 2.5+ ha il "thinking" ON di default che consuma `maxOutputTokens` → `_generate_gemini` forza `thinkingConfig.thinkingBudget=0`, altrimenti i messaggi escono troncati/vuoti. `gemini-2.0-flash` è dismesso (quota free 0).
- `AI_BASE_URL`: override endpoint OpenAI-compatible (vuoto = default provider)
- `AI_SYSTEM_PROMPT`: override system prompt completo (vuoto = usa default ottimizzato hardcoded)
- `AI_TEMPERATURE`: temperatura sampling, default `0.35` (più bassa = messaggi più consistenti)
- `SCRAPE_DAILY_LIMIT`: cap lookup `user_info_v1` per account/giorno durante la **Fase Bio** (default `300`). Override per-campagna disponibile su `campaigns.scrape_daily_limit`. La Fase Lista NON consuma cap (nessun `user_info`). Quando l'account raggiunge il cap, la Fase Bio ruota su un account alternativo o mette la campagna in pausa (`scrape_capped`); il cron `daily_reset` la riavvia dopo il reset del contatore se restano follower `pending`.
  - **Reset lazy date-aware (migrazione 018)**: il contatore `instagram_accounts.scrape_lookups_today` è etichettato con `scrape_lookups_date` (UTC "YYYY-MM-DD"). `has_scrape_budget`/`effective_scrape_lookups` (account_manager) trattano come 0 ogni contatore con data != oggi → il cap si auto-resetta al primo lookup del nuovo giorno SENZA dipendere dal cron `daily_reset` (che gira nel worker separato `cron_worker.CronWorkerSettings` alle 02:05 UTC e può non essere attivo overnight). Incremento via `bump_scrape_lookup` (in-memory date-aware) — un solo bump per lookup (i path legacy sommavano erroneamente anche `increment_scrape_lookup` → cap a metà). ⚠️ Il model dichiara `scrape_lookups_date`: applicare la migrazione 018 PRIMA di far girare il codice, altrimenti le SELECT su `instagram_accounts` falliscono (colonna mancante).
- **Fase Lista** — `LIST_PAGE_SIZE_MIN`/`LIST_PAGE_SIZE_MAX` (default `20`/`40`): dimensione pagina randomizzata passata come `max_amount` a `user_followers_v1_chunk`. **CRITICO**: con `max_amount=0` instagrapi drena l'intera lista in un burst `count=200` senza delay → challenge IG "comportamento automatizzato". Passando un `max_amount` piccolo ogni chiamata ritorna pochi utenti e i delay sotto agiscono (scroll umano). Questa è la vera leva anti-detection sulla lista (sostituisce il vecchio modello "il batch size lo decide IG").
- `LIST_PAGE_DELAY_MIN_SECONDS`/`LIST_PAGE_DELAY_MAX_SECONDS` (default `5`/`10`): delay lognormale tra pagine lista.
- `LIST_LONG_PAUSE_PROBABILITY` (default `0.06`) + `LIST_LONG_PAUSE_MIN/MAX_SECONDS` (default `30`/`60`): pausa lunga occasionale tra pagine (scroll che si ferma), simula distrazione umana.

I valori di timing DM (`MIN_DELAY_SECONDS`, `SESSION_*`, ecc.) e la differenza test/produzione stanno in [../architecture/PRINCIPI_ANTI_DETECTION.md](../architecture/PRINCIPI_ANTI_DETECTION.md).

---

## Avvio locale (sviluppo)

```bash
# 1. Redis (serve Docker Desktop attivo)
docker-compose up -d

# 2. Migrazioni DB (deploy step separato dal boot API)
cd backend
./venv/Scripts/activate        # Windows
source venv/bin/activate       # Unix
python -m scripts.migrate

# 3. Backend FastAPI
uvicorn app.main:app --reload --port 8000

# 4. ARQ Worker DM (in un secondo terminale)
cd backend
./venv/Scripts/activate
arq app.workers.task_queue.WorkerSettings

# 5. ARQ Cron Worker (in un terzo terminale)
cd backend
./venv/Scripts/activate
arq app.workers.cron_worker.CronWorkerSettings

# 6. Frontend Next.js (in un quarto terminale)
cd frontend
npm run dev
```

URLs: Dashboard → http://localhost:3000 | API Docs → http://localhost:8000/docs

---

## Prerequisiti per provider e browser

- Con `AI_PROVIDER=ollama`: il modello deve essere scaricato prima dell'uso: `ollama pull llama3.2`
- Con `AI_PROVIDER=groq`: registrarsi su `console.groq.com`, copiare la API key in `AI_API_KEY`. Gratuito.
- Con `AI_PROVIDER=gemini`: API key da `aistudio.google.com`. Gratuito (tier generoso).
- Patchright richiede il download di Chromium: `patchright install chromium`

---

Vedi anche: [SUPABASE_SETUP.md](SUPABASE_SETUP.md) · [SECRET_ROTATION.md](SECRET_ROTATION.md) · [TELEGRAM_SETUP.md](TELEGRAM_SETUP.md) · [PROXY_MOBILE_SETUP.md](PROXY_MOBILE_SETUP.md)
