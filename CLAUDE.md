# BOT OUTBOUND — CLAUDE.md

> **Per agenti AI**: leggi prima `INDEX.md` — contiene lo stato globale del progetto, i task aperti e l'ordine di lettura dei documenti.

Questo file contiene tutto il contesto architetturale e operativo che Claude deve conoscere per lavorare su questo progetto in qualsiasi conversazione futura.

---

## Regola obbligatoria: aggiornamento contesto a fine operazione

Prima di modificare un flusso esistente, rileggere il codice coinvolto e il contesto recente indicato da `INDEX.md`. I documenti possono essere indietro rispetto al codice: quando codice verificato e documentazione divergono, non rimuovere una miglioria o un guardrail solo per aderire al documento. Capire prima perche' quel codice esiste; se il codice locale non basta a distinguere una miglioria intenzionale da un residuo, chiedere chiarimento all'utente.

Oltre alla memoria persistente elencata sotto, riallineare nello stesso task i documenti di repository resi obsoleti dal codice verificato (`CLAUDE.md`, `INDEX.md`, `docs/project/PROGRESS.md` e il documento di architettura/setup coinvolto).

**Al termine di qualsiasi operazione** (fix, feature, refactor, debug), Claude DEVE aggiornare i file di memoria persistente in `C:\Users\39342\.claude\projects\d--BOT-OUTBOUND\memory\`:

1. **`project_state.md`** — aggiungere una sezione datata con: cosa è stato modificato, root cause se era un bug, file toccati, comportamento atteso dopo il fix.
2. **`MEMORY.md`** — verificare che l'indice rifletta eventuali nuovi file memory aggiunti.
3. Se l'operazione introduce una nuova architettura o pattern significativo → aggiornare anche questo `CLAUDE.md` nella sezione rilevante.

L'aggiornamento è **non opzionale** — è parte integrante del completamento di ogni task, non un'azione supplementare.

---

## Descrizione del progetto

**BOT OUTBOUND** è un agente di automazione per l'outreach su Instagram. Permette di:

1. Selezionare una pagina Instagram target dalla web app
2. Definire un messaggio base con template
3. Fare scraping dei **follower** o dei **following** della pagina target (incluse le bio) — modalità selezionabile per campagna
4. Generare messaggi personalizzati per ogni profilo usando un LLM (Ollama locale, Groq cloud, o Gemini cloud)
5. Inviare DM uno per uno simulando comportamento umano (timing randomizzato, rotazione account, browser realistico)

---

## Stack tecnologico

| Layer | Tecnologia |
|---|---|
| Backend API | Python 3.13 + FastAPI + Uvicorn |
| Database | **Supabase Postgres** (produzione, via `DATABASE_URL` + asyncpg) · SQLite + aiosqlite (WAL) come fallback dev locale |
| ORM / Migrations | SQLAlchemy 2.x async + Alembic |
| Task queue | ARQ (async Redis queue) |
| Cache/broker | Redis (via Docker) |
| AI messaggi | Multi-provider: Ollama locale · Groq cloud (free) · Gemini cloud (free) |
| Scraping IG | instagrapi (API privata Instagram) |
| Invio DM | Patchright (fork undetected di Playwright) + humanization-playwright |
| Crittografia | Fernet (cryptography lib) — password account mai in chiaro |
| Frontend | Next.js 14 (App Router) + TypeScript + Tailwind CSS + shadcn/ui |
| Data fetching | SWR (polling ogni 5-10s) |
| Logging | loguru |

---

## Struttura directory

```
d:\BOT OUTBOUND\
├── backend/
│   ├── venv/                         # Python 3.13 virtualenv
│   ├── app/
│   │   ├── main.py                   # FastAPI app, CORS, lifespan (create_tables)
│   │   ├── config.py                 # Pydantic Settings — legge .env dalla root
│   │   ├── database.py               # Async engine, Base, get_db, create_tables
│   │   ├── models/
│   │   │   ├── account.py            # InstagramAccount + AccountStatus enum
│   │   │   ├── campaign.py           # Campaign + CampaignStatus enum
│   │   │   ├── campaign_account.py   # CampaignAccount (join table campaigns ↔ accounts)
│   │   │   ├── follower.py           # Follower + FollowerStatus enum
│   │   │   ├── message.py            # Message + MessageStatus enum
│   │   │   ├── activity_log.py       # ActivityLog
│   │   │   └── global_contact.py     # GlobalContact (deduplicazione cross-campaign)
│   │   ├── schemas/
│   │   │   ├── account.py            # AccountCreate, AccountUpdate, AccountResponse
│   │   │   ├── campaign.py           # CampaignCreate, CampaignUpdate, CampaignResponse
│   │   │   ├── follower.py           # FollowerResponse, FollowerListResponse
│   │   │   ├── message.py            # MessageResponse, MessageListResponse
│   │   │   └── dashboard.py          # DashboardStats, ActivityLogResponse, TimelineResponse
│   │   ├── api/
│   │   │   ├── accounts.py           # CRUD + login + manual-login + metrics + dm-count + force-cancel-cooldown
│   │   │   ├── campaigns.py          # CRUD + start/pause/resume/stop + pre-generate + approval-queue + A/B stats + events
│   │   │   ├── campaign_accounts.py  # CRUD account assegnati a campagna
│   │   │   ├── followers.py          # Lista paginata + skip + regenerate + requeue
│   │   │   ├── messages.py           # Log + retry
│   │   │   ├── dashboard.py          # Stats, activity feed, timeline
│   │   │   ├── leads.py              # Lead database + export CSV
│   │   │   └── health.py             # Health check sistema
│   │   ├── services/
│   │   │   ├── account_manager.py    # Rotazione account, warm-up, cooldown, reset giornaliero; has_scrape_budget/increment_scrape_lookup
│   │   │   ├── global_contact_service.py  # upsert_lead + merge contatti cross-campagna in global_contacts
│   │   │   ├── scraper.py            # instagrapi: login (session restore only), scrape follower/following, fetch bio
│   │   │   ├── ai_personalizer.py    # Multi-provider LLM: generate, validate, fallback, batch, approval sampling
│   │   │   ├── dm_sender.py          # Patchright: invio singolo DM
│   │   │   ├── campaign_orchestrator.py  # Loop principale campagna (multi-worker)
│   │   │   ├── manual_login.py       # Login browser manuale (Patchright)
│   │   │   ├── reply_checker.py      # Cron: scansione inbox DM per risposte
│   │   │   ├── human_behavior.py     # Sessioni, timing, finestra oraria
│   │   │   └── campaign_control.py   # Controlli condivisi pausa/ripresa campagna (web + Telegram)
│   │   ├── workers/
│   │   │   ├── task_queue.py         # ARQ WorkerSettings, funzioni cron
│   │   │   ├── scrape_worker.py      # Task: scrape_followers_task
│   │   │   └── message_worker.py     # Task: send_message_task
│   │   ├── browser/
│   │   │   ├── context_manager.py    # Browser pool + mutex per-account + fingerprinting
│   │   │   ├── instagram_page.py     # Page Object Model Instagram (header-scoped selectors)
│   │   │   └── fingerprint.py        # Fingerprint deterministico (viewport, UA, timezone)
│   │   └── utils/
│   │       ├── crypto.py             # Fernet encrypt/decrypt
│   │       ├── timing.py             # Log-normal delay generator
│   │       ├── exceptions.py         # Custom exceptions hierarchy
│   │       ├── retry.py              # Retry decorator con exponential backoff
│   │       ├── events.py             # Sistema eventi Redis per live log frontend
│   │       └── contact_extract.py    # Estrazione contatti IG (campi business + regex bio + WhatsApp)
│   ├── alembic/
│   │   ├── env.py
│   │   └── versions/
│   ├── tests/
│   ├── data/                         # Creata a runtime
│   │   ├── bot.db                    # SQLite (solo dev locale; produzione = Supabase)
│   │   └── browser_profiles/         # Profili Chromium per account
│   ├── pyproject.toml
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── app/                      # Next.js App Router
│   │   │   ├── layout.tsx
│   │   │   ├── page.tsx              # Dashboard home
│   │   │   ├── campaigns/            # Lista + detail + new
│   │   │   ├── accounts/             # Gestione account IG
│   │   │   ├── messages/             # Log messaggi
│   │   │   └── settings/             # Impostazioni globali
│   │   ├── components/
│   │   │   ├── ui/                   # shadcn/ui primitives
│   │   │   ├── layout/               # Sidebar, Header
│   │   │   ├── campaigns/            # CampaignCard, CampaignForm, ProgressBar
│   │   │   ├── accounts/             # AccountCard, AccountForm, HealthBadge
│   │   │   └── dashboard/            # StatsGrid, ActivityFeed, CampaignChart
│   │   ├── lib/
│   │   │   ├── api.ts                # Fetch wrapper → http://localhost:8000/api
│   │   │   └── types.ts              # TypeScript types
│   │   └── hooks/                    # SWR hooks
│   ├── package.json
│   └── next.config.js
├── docker-compose.yml                # Redis su porta 6379
├── .env                              # Secrets (NON committare)
├── .env.example                      # Template senza secrets
├── CLAUDE.md                         # Questo file
├── docs/
│   ├── audits/                       # Audit tecnici
│   ├── architecture/                 # Note architetturali e anti-detection
│   ├── guides/                       # Guide utente
│   ├── project/                      # Stato progetto e progress log
│   ├── setup/                        # Setup servizi e rotazione segreti
│   └── superpowers/plans/            # Piani operativi
├── data/
│   └── profiles/                     # Liste profili/account di lavoro
├── backups/                          # Backup locali e vecchi .env
├── start.bat                         # Avvio Windows
└── start.sh                          # Avvio Unix/WSL
```

---

## Database schema

### `instagram_accounts`
Stato degli account Instagram usati per inviare DM.
- `status` enum: `active | warming_up | cooldown | banned | challenge_required | disabled`
- `warmup_day`: 0 = non in warm-up. Incrementato ogni giorno. Controlla il limite giornaliero dinamico.
- `session_data`: JSON serializzato di instagrapi (evita re-login)
- `encrypted_password`: Fernet-encrypted, mai in chiaro
- `scrape_lookups_today`: contatore lookup `user_info_by_username_v1` eseguiti oggi; resettato dal cron `daily_reset`. Usato per il cap anti-ban (vedi `SCRAPE_DAILY_LIMIT`).

### `campaigns`
Una campagna = una sorgente di profili + un template messaggio.
- `source_type`: `'scrape'` (default) | `'import'`. `scrape` = raccoglie follower/following di `target_username`; `import` = profili caricati da file (vedi `imported_profiles`). Per `import`, `target_username` è NULL (reso nullable in migrazione 013); UI/query che lo assumono presente devono fare guardia su `source_type`.
- `status` enum: `draft → scraping → scraping_break | scraping_and_running → ready → running → paused → completed | error`
  - `scraping_break`: scraper in pausa sessione (con countdown), riprendibile manualmente
  - `scraping_and_running`: scraper + worker DM attivi simultaneamente (account separati per ruolo)
- `total_followers` / `messages_sent/failed/pending`: contatori denormalizzati per performance UI
- `base_message_template`: template principale (ora **nullable** — NULL consentito quando `messaging_enabled=False`; non può essere vuoto/NULL se `messaging_enabled=True`)
- `message_template_b`: template B opzionale per A/B testing (M10)
- `daily_limit`: limite DM/giorno per l'intera campagna
- `require_approval` + `approval_sample_size`: approvazione messaggi a campione (M15)
- `scrape_mode`: `'followers'` (default) | `'following'` — controlla se lo scraper raccoglie i follower della pagina target o i profili che essa segue
- `scrape_session_size`: profili per sessione prima della pausa (default 250)
- `scrape_break_minutes_min/max`: durata pausa sessione in minuti (default 30/45)
- `bio_fetch_delay_min/max`: delay tra fetch bio in secondi (default 5/8)
- `auto_generate`: se True, i worker DM generano messaggi AI on-the-fly (no pre-gen manuale)
- `scrape_break_until`: timestamp fine pausa sessione attiva (null se non in pausa)
- `scrape_break_prev_status`: status da ripristinare al termine della pausa
- `messaging_enabled`: bool (default True) — se False, la campagna fa solo scraping/raccolta contatti senza inviare DM; `/start` e `/start-dm-auto` restituiscono 400 se disattivata. Campagne scraping-only terminano in `completed` al termine dello scraping.
- `scrape_daily_limit`: int nullable — override del cap lookup per questa campagna (sovrascrive `SCRAPE_DAILY_LIMIT` da `.env`). NULL = usa il default globale.

### `campaign_accounts`
Join table campagne ↔ account Instagram.
- `daily_limit_override`: override del limite giornaliero per questo account su questa campagna
- `is_active`: flag per abilitare/disabilitare l'account su questa campagna
- `role`: `'scraping'` | `'dm'` | `'both'` (default) — ruolo account nella campagna. Scraper usa solo `scraping`/`both`. Worker DM usano solo `dm`/`both`.

### `followers`
Ogni riga è un follower della pagina target in una campagna specifica.
- `status` enum: `pending → bio_scraped → message_generated → pending_approval → sent | failed | skipped | replied`
- Unique constraint: `(campaign_id, ig_user_id)` — previene duplicati nella stessa campagna
- `locked_by_account_id` + `locked_at`: optimistic locking per multi-worker (auto-released dopo 20 min)
- Colonne contatto (aggiunte in migrazione 014): `phone`, `email`, `whatsapp` (stringhe nullable), `bio_links` (JSON nullable — lista link dal profilo IG), `contact_source` (JSON nullable — quale campo/metodo ha estratto ogni dato), `contact_extra` (JSON nullable — dati grezzi aggiuntivi). Popolati da `contact_extract.py` a scrape-time o a resolve-time (import).

### `imported_profiles`
Tabella di staging per la modalità `source_type='import'` (migrazione 013). Ogni riga = un profilo IG fornito dall'utente via file, in attesa di risoluzione in `Follower`. Serve perché `Follower.ig_user_id` è NOT NULL + unique ma all'import si ha solo lo username (il `pk` arriva dopo la call IG).
- `status` enum: `pending → resolved | not_found | private | error`
- `raw_input`: riga originale del file; `username`: username normalizzato (lowercase)
- `ig_user_id`: popolato dopo la risoluzione (null finché `pending`)
- Unique constraint: `(campaign_id, username)` — dedup interno alla campagna
- Risolto dal worker `resolve_imports_task` (`app/services/import_resolver.py`): `user_info_by_username_v1` → crea `Follower(bio_scraped)`; riusa login/rotazione-429/session-break dello scraper. Profilo privato → `Follower` creato comunque. La dedup `global_contacts` NON avviene qui (solo a send-time).

### `messages`
Ogni DM (generato o inviato) è una riga separata.
- Collegato a follower + account che ha inviato + campagna
- `template_variant`: 'a' o 'b' per A/B testing (M10)
- Permette retry granulare

### `global_contacts`
Lead database + deduplicazione cross-campagna. Previene di inviare DM due volte allo stesso utente. Un profilo diventa un "lead visto" (`last_contacted_at=NULL`) nel momento dello scraping, anche se la messaggistica è disattivata — la colonna `last_contacted_at` viene popolata solo al primo invio DM riuscito.
- `ig_user_id` UNIQUE
- `username`, `full_name`, `biography`: dati profilo lead aggiornati ad ogni invio
- `contacted_by_campaign_ids`: JSON array di campaign_id (legacy, per backward compat)
- `contact_history`: JSON array ricco — ogni entry `{campaign_id, campaign_name, account_id, account_username, contacted_at}`
- Le colonne nuove (`username`, `full_name`, `biography`, `contact_history`) sono aggiunte via migrazione inline al boot (`ALTER TABLE ADD COLUMN` con try/except in `database.py`)
- Colonne contatto (aggiunte in migrazione 014): `phone`, `email`, `whatsapp`, `external_url` (stringhe nullable), `bio_links` (JSON nullable), `contact_source` (JSON nullable), `contact_extra` (JSON nullable). Merge cross-campagna con gap-fill: un campo viene aggiornato solo se era NULL e il nuovo valore è non-vuoto.
- `scrape_sources`: JSON array NOT NULL (default `[]`) — elenco delle sorgenti (campaign_id + timestamp) da cui il profilo è stato visto durante lo scraping, anche senza DM inviato.
- `first_seen_at`: timestamp del primo scraping (NULL su righe pre-014).

### `activity_logs`
Audit trail di tutte le azioni significative: login, scrape, dm_sent, dm_failed, rate_limited, challenge, cooldown_start/end, account_banned.

---

## Configurazione (.env)

Il file `.env` va messo nella **root del progetto** (`d:\BOT OUTBOUND\.env`).
Alembic e FastAPI lo leggono tramite Pydantic Settings con `env_file=".env"`.

Variabili chiave:
- `SECRET_KEY`: chiave Fernet generata con `from cryptography.fernet import Fernet; Fernet.generate_key()`
- `DATABASE_URL`: **in produzione punta a Supabase Postgres** (`postgresql+asyncpg://...@...pooler.supabase.com...`). Il codice aggiunge automaticamente parametri safe per Supabase Pooler/PgBouncer (`prepared_statement_cache_size=0`, `statement_cache_size=0`, unique prepared statement names, `NullPool`) per evitare `DuplicatePreparedStatementError`.
  - Fallback dev locale: `sqlite+aiosqlite:///./data/bot.db` (relativo a `backend/`). Il codice mantiene i branch SQLite (vedi `app/utils/db_dialect.py`), ma il deployment reale è su Supabase.
  - **Le migrazioni Alembic girano contro Supabase** (`python -m scripts.migrate`). Attenzione: una connessione `idle in transaction` lasciata aperta da un processo bot morto tiene un lock su `campaigns`/`followers` e fa andare in timeout gli `ALTER TABLE` — fermare il bot e/o terminare il backend zombie prima di migrare.
- `OLLAMA_MODEL`: nome modello Ollama (usato solo se `AI_PROVIDER=ollama`)
- `AI_PROVIDER`: `ollama` | `groq` | `gemini` — seleziona provider LLM
- `AI_API_KEY`: API key del provider cloud (Groq: `gsk_...`, Gemini: `AIza...`)
- `AI_MODEL`: modello specifico (vuoto = default provider: Groq→`llama-3.3-70b-versatile`, Gemini→`gemini-2.0-flash`)
- `AI_BASE_URL`: override endpoint OpenAI-compatible (vuoto = default provider)
- `AI_SYSTEM_PROMPT`: override system prompt completo (vuoto = usa default ottimizzato hardcoded)
- `AI_TEMPERATURE`: temperatura sampling, default `0.35` (più bassa = messaggi più consistenti)
- `SCRAPE_DAILY_LIMIT`: cap lookup `user_info_by_username_v1` per account/giorno durante scraping (default `180`). Override per-campagna disponibile su `campaigns.scrape_daily_limit`. Quando l'account raggiunge il cap, lo scraper ruota su un account alternativo o mette la campagna in pausa (`scrape_capped`).

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

---

## Principi anti-detection (IMPORTANTE)

Non modificare il comportamento di timing o simulazione umana senza considerare questi principi:

1. **Mai delay uniformi** — usare sempre distribuzioni log-normali con sigma alto (0.7) per più varianza naturale
2. **Sessioni limitate** — 5-12 DM per sessione (test) / 10-20 (produzione), poi pausa obbligatoria
3. **Finestra oraria** — nessun invio fuori da `ACTIVE_HOURS_START` - `ACTIVE_HOURS_END`
4. **Profili browser persistenti** — ogni account ha un profilo Chromium dedicato, NON aprire in incognito
5. **Warm-up graduale** — account nuovi iniziano con 3-5 DM/giorno e aumentano nel tempo
6. **Deduplicazione obbligatoria** — controllare sempre `global_contacts` prima di inviare
7. **Scroll-to-top prima del click** — dopo `_simulate_browsing`, risalire sempre in cima alla pagina prima di cliccare "Message"
8. **Ordine follower randomizzato** — `ORDER BY func.random()` per non contattare sempre nello stesso ordine
9. **Typing lognormale** — delay per tasto da distribuzione lognormale + pause tra parole (15% prob) + micro-pause rare
10. **IP diversificazione** — con 3+ account è necessario usare proxy distinti (vedi sezione Scala)
11. **Pause sessione vincolanti** — un recap "riparte alle HH:MM" non deve essere aggirato da recovery/reenqueue; prima di riaccodare verificare Redis (`job`, `retry`, `in-progress`) e lease account.
12. **Stories browsing consentito ma reversibile** — mantenere la visita alle storie per naturalezza, ma chiudere sempre il viewer prima dei controlli DM; non cercare input DM dentro il viewer storie.

### Valori timing

| Parametro | Test aggressivo | Produzione consigliata |
|---|---|---|
| `MIN_DELAY_SECONDS` | 10 | 120 |
| `MAX_DELAY_SECONDS` | 45 | 480 |
| `SESSION_MIN_MESSAGES` | 5 | 10 |
| `SESSION_MAX_MESSAGES` | 12 | 20 |
| `SESSION_BREAK_MIN_MINUTES` | 10 | 30 |
| `SESSION_BREAK_MAX_MINUTES` | 25 | 60 |

---

## Architettura AI (ai_personalizer.py)

Il layer AI supporta tre provider configurabili via `.env`:

| Provider | Config | Default model | Note |
|---|---|---|---|
| `ollama` | nessuna API key | `OLLAMA_MODEL` | locale, lento, qualità bassa su modelli piccoli |
| `groq` | `AI_API_KEY=gsk_...` | `llama-3.3-70b-versatile` | gratis, OpenAI-compatible, raccomandato |
| `gemini` | `AI_API_KEY=AIza...` | `gemini-2.0-flash` | gratis, REST API propria |

### Parametri chiave
- `AI_TEMPERATURE=0.35` — bassa per messaggi B2B consistenti (non alzare oltre 0.5)
- `AI_SYSTEM_PROMPT` — se vuoto, usa il default ottimizzato in `ai_personalizer.py:DEFAULT_SYSTEM_PROMPT`
- Il system prompt default: ruolo B2B, regole numerate per priorità, "preserva struttura template", "grammaticalmente corretto", "non inventare dalla bio"

### Flusso generazione
1. `generate_message()` → legge `settings.ai_provider` → branch sul provider
2. `_build_user_prompt()` → costruisce il prompt utente con template + bio + contesto campagna
3. `_get_system_prompt()` → usa `AI_SYSTEM_PROMPT` da .env oppure `DEFAULT_SYSTEM_PROMPT`
4. `_validate_message()` → strip virgolette, collapse `\n` (Instagram invia su Enter), truncate, fallback

---

## Architettura browser (Patchright)

Il layer browser in `app/browser/` gestisce:

- **`context_manager.py`**: pool profili Chromium, 1 profilo per account, canvas noise injection
- **`fingerprint.py`**: fingerprint deterministico per account (viewport, UA, timezone, locale)
- **`instagram_page.py`**: Page Object Model per Instagram web

### Flusso `send_dm`:
1. `page.goto(profile_url)` → carica profilo target
2. `_simulate_browsing()` → scroll randomizzato (4 tipi: scroll piccolo, scroll grande, pausa lettura, hover) per `pre_dm_browse_seconds()` secondi (lognormale ~12s)
3. `window.scrollTo(0,0)` → risale in cima (il pulsante Message è nell'header del profilo)
4. Click su `div[role="button"]:text-is("Message")` (match esatto, non `has-text`)
5. `wait_for_url('/direct/')` → attende navigazione alla thread DM
6. Dismiss popup vari ("Not Now", "Cancel", ecc.)
7. `_human_type()` → typing lognormale con pause tra parole
8. `Enter` → invio

---

## Scala e parallelismo

### Multi-account per campagna (Fase 7A — ✅ IMPLEMENTATA)
- 1 ARQ job per account assegnato, deduplicato con `_job_id=worker:{campaign_id}:{account_id}`
- Claiming atomico via `UPDATE WHERE locked_by_account_id IS NULL` (Postgres/Supabase; SQLite WAL in dev)
- Crash recovery: stale lock timeout 20min + cron 15min; lo startup guard del worker DM pausa lavoro attivo stale da processi precedenti.
- Mutex asyncio per-account in `context_manager.py` — 1 browser alla volta
- Campaign daily limit live query (non contatore stale)
- I worker DM sono batch short-lived: a fine sessione/budget sollevano `Retry(defer=...)` invece di dormire dentro ARQ.
- Durante il defer di sessione il worker mantiene un lease account fino a pochi secondi prima della ripartenza, cosi' eventuali job duplicati immediati escono senza inviare ma il job ARQ differito puo' ripartire all'orario previsto.
- Il cron dedicato recupera ogni 5 minuti i `messages.status='sending'` stale; dopo recovery/retry/giveup riaccoda il worker solo se campagna, account e assegnazione DM sono ancora utilizzabili e non esiste gia' un job Redis (`arq:job`, `arq:retry`, `arq:in-progress`) per lo stesso campaign/account.

### Scraping + DM parallelo con ruoli account (✅ IMPLEMENTATA)
- Account role `scraping | dm | both` su `campaign_accounts` — scraper non usa mai account `dm`
- Stato composto `scraping_and_running`: scraper e worker DM girano simultaneamente
- Stato `scraping_break`: pausa sessione con countdown UI e "Riprendi subito"
- Session break configurabile per-campagna (`scrape_session_size`, `break_min/max`)
- Bio fetch delay configurabile (`bio_fetch_delay_min/max`, default 5-8s)
- Stagger automatico worker DM (0-15 min random offset) per desincronizzare session break
- `auto_generate=True`: worker DM generano messaggi AI on-the-fly su follower `bio_scraped`
- Endpoint: `POST /campaigns/{id}/start-dm-auto`, `POST /campaigns/{id}/resume-break`

### Import profili da lista (✅ IMPLEMENTATA)
- Alternativa allo scraping: la campagna parte da una lista di profili IG caricata da file (`.txt`/`.csv`) invece che dai follower/following di una pagina. Caso d'uso: il cliente ha già selezionato i profili.
- `campaigns.source_type='import'` + tabella staging `imported_profiles`. Worker dedicato `resolve_imports_task` (`app/services/import_resolver.py`) risolve ogni username via `user_info_by_username_v1` (1 call → pk + bio), creando `Follower(bio_scraped)`. Riusa login/anti-detection/session-break/kill-switch dello scraper. Il flusso AI + invio DM a valle è invariato.
- Parser URL/username puro: `app/utils/ig_username.py` (gestisce URL completi, `@handle`, username nudo, prima colonna CSV, scarta path non-profilo come `/p/`, `/reel/`).
- Endpoint: `POST /campaigns/{id}/import-profiles` (upload multipart, solo in `draft`), `GET /campaigns/{id}/import-status` (contatori per-stato). `start-scrape` dirama su `source_type` → `enqueue_resolve` (job id `resolve:{campaign_id}`). Serve comunque un account con ruolo `scraping`/`both` per le call IG.
- Frontend: toggle "Sorgente: Scraping pagina | Lista importata" nel form nuova campagna + pannello contatori (pending/resolved/not_found/private/error) nel dettaglio.

### Control-plane remoto e kill-switch (✅ IMPLEMENTATO)
- Kill-switch globale in `bot_state`: `halted=True` blocca scraper e worker DM sui check interni. Endpoint web admin: `POST /admin/halt`, `POST /admin/resume`.
- Sidebar web admin: mostra sempre lo stato del kill-switch; quando spento offre **Blocca tutto**, quando acceso offre **Sblocca**.
- Telegram separa controlli campagna e blocco globale:
  - `/pause` mostra bottoni inline con le campagne attive e mette in pausa solo la campagna selezionata.
  - `/resume` mostra bottoni inline con le campagne in pausa e riprende solo la campagna selezionata.
  - `/halt [motivo]` attiva il kill-switch globale di emergenza.
  - `/unhalt` disattiva il kill-switch globale e riaccoda solo il lavoro ancora in stato attivo.
- `campaign_control.py` centralizza pausa/ripresa per API web e Telegram, con pre-check Redis prima di portare una campagna a running.
- Problemi su singolo account non fermano tutto il bot: `cooldown`, `challenge_required` e `banned` isolano l'account; vengono pausate solo le campagne che non hanno altri account DM utilizzabili. Il kill-switch resta per problemi sistemici o comando manuale.

### Scraping avanzato + raccolta contatti (✅ IMPLEMENTATA)
- **Estrazione contatti in 1 call IG**: modulo puro `app/utils/contact_extract.py` legge campi business IG (`public_phone_number`, `public_email`, `whatsapp_number`, link profilo) + regex su bio (telefono, email, WhatsApp) e restituisce un `ContactData`. Consumato da scraper (`scraper.py`) e resolver import (`import_resolver.py`).
- **Lead visto a scrape-time**: ogni profilo scrapato/risolto viene fatto confluire in `global_contacts` via `app/services/global_contact_service.py` (`upsert_lead` + merge gap-fill). Il record diventa visibile nei lead anche prima che venga inviato qualsiasi DM (`last_contacted_at=NULL`). I campi contatto vengono arricchiti a send-time in `campaign_orchestrator._mark_globally_contacted`.
- **Messaggistica opzionale**: toggle `campaigns.messaging_enabled` — se False, la campagna termina come `completed` dopo lo scraping senza inviare DM. Guard in `/start` e `/start-dm-auto` (HTTP 400 se disattivata o template mancante). Frontend: toggle "Invia messaggi" nel form nuova campagna.
- **Cap anti-ban per-account**: `SCRAPE_DAILY_LIMIT` (env) + `campaigns.scrape_daily_limit` (override). Contatore `instagram_accounts.scrape_lookups_today` aggiornato dopo ogni `user_info` call; `has_scrape_budget`/`increment_scrape_lookup` in `account_manager.py`. Al raggiungimento del cap, lo scraper/resolver ruota su account alternativo o mette la campagna in pausa (`scrape_capped`). Contatore resettato dal cron `daily_reset`.
- **Export leads filtrabile**: endpoint `/leads` e `/leads/export` estesi con filtri `campaign_ids[]`, `scraping_account_ids[]`, `has_phone`, `has_email` — nessuna cross-client data leak. Frontend: multi-select filtri + colonne contatto nella pagina leads.
- **Multi-account round-robin scraping** (Approccio C): con 2+ account `scraping`/`both` su una campagna, il bio-fetch alterna gli account per-lead (`ScrapingPool` in `app/services/scraping_pool.py`), condividendo il carico dall'inizio (prima era sequenziale: A fino al cap, poi B). Tutti gli account vengono pre-loggati una volta e tenuti in memoria (1 slot scraping ciascuno, 1 client con proxy proprio); job singolo seriale, nessun worker parallelo. La paginazione lista resta su 1 account (chiamate cheap); il bio-fetch e la rotazione 429/soft-block usano `pool.next()` (niente re-login per switch). Cap per-account via `pool.next` (salta i capped; tutti a cap → `ScrapeBudgetError`). Il bump in-memory di `scrape_lookups_today` è visibile a `pool.next` grazie a `expire_on_commit=False`. Break **campagna-level** invariato (box "Pausa sessione" + countdown UI preservati). ⚠️ Il delay `bio_fetch_delay` è GLOBALE per-lead: con N account ogni account attende ~N× il valore — la UI lo segnala (helper text nel form nuova campagna e nel modale impostazioni). Compat mono-account: pool di 1 elemento = comportamento identico a prima. **Anche il resolver import** (`import_resolver.py`) usa ora lo stesso `ScrapingPool` round-robin per-riga (rotazione 429/cap via `pool.next`, break campagna-level invariato).
- **Test connessione per-account**: `app/utils/proxy_probe.py` (`probe_egress(proxy)`) + endpoint `POST /accounts/{id}/test-connection` → IP/ISP/ASN/mobile reali di uscita via proxy dell'account (o WiFi se nessun proxy). Frontend: bottone "Testa IP" + pannello su ogni card account. Verifica al volo che il proxy esca su IP mobile diverso dal WiFi.
- **Log per-lead round-robin**: `import_resolver.py` e `scraper.py` loggano per ogni lead l'account usato (`[Import] @user -> status via @account` / `[Scraper] @user bio via @account`). ASCII-only (console Windows cp1252).
- **Restart da errore**: `POST /campaigns/{id}/start-scrape` accetta ora anche `status='error'` (oltre a `draft`) — riprende scraping/risoluzione senza perdere il progresso (import: riparte dalle righe `imported_profiles` ancora `pending`, le `resolved` restano; scrape: riparte dal cursore con dedup follower). Frontend: bottone "Riprendi risoluzione"/"Riavvia scraping" sullo stato `error` (oltre a Reset). Caso d'uso tipico: proxy/USB caduto a metà → errore → fix connessione → restart.
- Piani/spec: `docs/superpowers/plans/` e `docs/superpowers/specs/` (branch `feature/advanced-scraping`). Migrazione: `014_advanced_scraping_contacts.py`.

### Multi-campagna parallela (Fase 7B — da implementare)
- Aggiungere `current_campaign_id` a `InstagramAccount`
- UI di assegnazione account → campagna
- ARQ `max_jobs = 10` già sufficiente per N campagne in parallelo

### IP diversificazione (necessaria con 3+ account)

| Approccio | Trust IG | Costo | Implementazione |
|---|---|---|---|
| ISP proxy residenziale | ★★★★ | €2-5/IP/mese | Campo `proxy` su account IG |
| Mobile proxy 4G/5G | ★★★★★ | €30-80/IP/mese | Campo `proxy` su account IG |
| Android personale come proxy | ★★★★★ | €5-10 app + SIM | App Proxidize/iProxy sul telefono |

**Dispositivi Android come proxy** (raccomandato se si hanno dispositivi disponibili):
- L'app gira sul telefono Android e lo trasforma in un proxy 4G
- Il bot configura `proxy=http://dispositivo:porta` nel campo account
- Il traffico IG esce dall'IP mobile del dispositivo (identico a un vero utente mobile)
- Nessuna riscrittura del bot richiesta

---

## Convenzioni codice

- **Async everywhere**: tutti i servizi, worker e route handler sono `async def`
- **Dependency injection**: usare `Depends(get_db)` per le sessioni DB, mai creare sessioni manualmente nelle route
- **Errors**: sollevare `HTTPException` nelle route, eccezioni custom in `utils/exceptions.py` nei servizi
- **No ORM lazy loading**: usare `selectinload` o `joinedload` esplicitamente dove necessario
- **Logging**: usare `loguru` — mai `print()` in produzione
- **Secrets**: mai loggare password, session_data o il SECRET_KEY

---

## Dipendenze principali

```
fastapi, uvicorn[standard]
sqlalchemy[asyncio], asyncpg (Supabase/Postgres), aiosqlite (dev), alembic
pydantic>=2.7, pydantic-settings
cryptography (Fernet)
httpx (Ollama client)
arq (task queue)
loguru
instagrapi (Fase 2+)
patchright, humanization-playwright (Fase 4+)
```

---

## Fasi di sviluppo (stato)

Vedi `docs/project/PROGRESS.md` per lo stato aggiornato di ogni fase.

| Fase | Descrizione | Stato |
|---|---|---|
| 1 | Foundation: backend, DB, models, schemas, API accounts | ✅ Completata |
| 2 | Scraping follower/following con instagrapi (modalità selezionabile per campagna) | ✅ Completata |
| 3 | AI personalizzazione messaggi (multi-provider: Ollama/Groq/Gemini) | ✅ Completata |
| 4 | Engine invio DM (Patchright + anti-detection) | ✅ Completata |
| 5 | Frontend Next.js dashboard | ✅ Completata |
| 6 | Hardening, logging, test | Parziale (logging OK, test da fare) |
| 7A | Multi-account per campagna | ✅ Completata |
| 7C | Lead database + export CSV | ✅ Completata |
| 7D | Scraping + DM parallelo, ruoli account, session break configurabile | ✅ Completata |
| 7E | Import profili da lista (source_type=import, imported_profiles, resolve worker) | ✅ Completata |
| 7F | Scraping avanzato: contatti (telefono/email/whatsapp/link) + messaggistica opzionale + cap scraping | ✅ Completata |
| 7B | Multi-campagna parallela | Da fare |

---

## Note operative

- L'account Instagram usato per lo scraping **non deve** necessariamente essere lo stesso che invia i DM
- Raccomandato usare **proxy residenziali** per account con alto volume (non incluso nel MVP)
- Instagram limita ~50-100 DM/giorno per account. Non superare mai questo limite, meglio stare su 20-30
- Con `AI_PROVIDER=ollama`: il modello deve essere scaricato prima dell'uso: `ollama pull llama3.2`
- Con `AI_PROVIDER=groq`: registrarsi su `console.groq.com`, copiare la API key in `AI_API_KEY`. Gratuito.
- Con `AI_PROVIDER=gemini`: API key da `aistudio.google.com`. Gratuito (tier generoso).
- Patchright richiede il download di Chromium: `patchright install chromium`
