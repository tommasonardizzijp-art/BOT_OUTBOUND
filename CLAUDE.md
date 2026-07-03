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
│   │   │   ├── lead_qualification.py # Target profile + run/export qualifica lead
│   │   │   └── health.py             # Health check sistema
│   │   ├── services/
│   │   │   ├── account_manager.py    # Rotazione account, warm-up, cooldown, reset giornaliero; has_scrape_budget/increment_scrape_lookup
│   │   │   ├── global_contact_service.py  # upsert_lead + merge contatti cross-campagna in global_contacts
│   │   │   ├── lead_qualification.py # scoring deterministico + AI compiler/classifier per lead
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
│   │   │   ├── lead_qualification_worker.py # Task batch qualifica lead
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
- `status` enum: `draft → listing → listing_break → ready → scraping → scraping_break | scraping_and_running → running → paused → completed | error`
  - `listing`: **Fase Lista** (two-phase) — raccolta info base dei follower a blocchetti paced, nessun `user_info_v1` (no consumo cap)
  - `listing_break`: Fase Lista in pausa sessione (con countdown), riprendibile manualmente
  - `scraping`: per `source_type='scrape'` ora indica la **Fase Bio** (estrazione bio/contatti dai follower `pending`); per `source_type='import'` indica la risoluzione
  - `scraping_break`: scraper/bio in pausa sessione (con countdown), riprendibile manualmente
  - `scraping_and_running`: legacy scraper + worker DM attivi simultaneamente (account separati per ruolo)
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
- `list_target`: int nullable — target di follower da raccogliere nella Fase Lista (NULL = tutta la lista). Stop manuale sempre disponibile.
- `bio_target`: int nullable — target di bio da estrarre nella Fase Bio (NULL = tutti i `pending`). Stop manuale sempre disponibile.

### `campaign_accounts`
Join table campagne ↔ account Instagram.
- `daily_limit_override`: override del limite giornaliero per questo account su questa campagna
- `is_active`: flag per abilitare/disabilitare l'account su questa campagna
- `role`: capability componibili (stringa `String(16)`, default `both`). Base: `'scraping'` (solo bio), `'dm'` (solo invio), `'both'`. Capability **inbox** (listing dei DM-thread, solo `scrape_mode='dm_threads'`), combinabile: `'inbox'`, `'inbox_scraping'`, `'inbox_dm'`, `'inbox_both'`. **Una sola** capability inbox per campagna (un account legge una sola inbox DM); gli account scraping/dm sono illimitati → bio/DM si spalmano. Fonte di verità unica: `app/utils/roles.py` (`SCRAPE_ROLES`/`DM_ROLES`/`INBOX_ROLES` + `can_scrape`/`can_dm`/`is_inbox`); **mai** filtrare per tuple inline. Scraper (bio) usa `SCRAPE_ROLES`, worker DM `DM_ROLES`, il listing inbox `INBOX_ROLES` (esattamente 1).

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

### `lead_target_profiles`, `lead_qualification_runs`, `lead_qualifications`
Sezione "Qualifica lead" (migrazione 015). Lavora solo sui lead consolidati in `global_contacts`, non sui `followers` grezzi.
- `lead_target_profiles`: target riutilizzabili descritti in linguaggio naturale, con `compiled_rules` JSON generato/modificabile dall'AI e `rules_hash` stabile.
- `lead_qualification_runs`: batch filtrati su `global_contacts`, con stato `queued|running|completed|failed|cancelled`, filtri JSON, contatori progressivi e skip dei lead gia classificati con stesso target+rules_hash. Salva uno **snapshot** di target/regole/soglie al momento della run (`target_name`, `target_description`, `compiled_rules`, `pass/reject_threshold`, `ai_review_min/max_score`) — colonne aggiunte in **migrazione 017** (la 015 le aveva omesse → drift che bloccava l'INSERT della run).
- `lead_qualifications`: risultati storici per lead+target+run con `deterministic_score`, `ai_score`, `final_score`, stato `match|no_match|ambiguous|error`, segnali JSON e `reason` opzionale.
- La vista operativa usa l'ultimo risultato per coppia `(target_profile_id, global_contact_id)` senza cancellare lo storico run.
- **Scoring (redesign 2026-06-11, recall-first)**: `score_lead` è deterministico e tarato perché **1 keyword di nicchia corretta = match diretto, senza AI**. Default: `pass_threshold=10`, `reject_threshold=0`, `ai_review=[1,9]`, `positive_term_bonus=10`.
  - `positive_terms`/`strong_terms` = keyword SPECIFICHE (peso ≥10) → da sole fanno **match**. `positive_concepts` = parole GENERICHE (uomo, donna, …) → contano **una volta sola, peso fisso 5**, non superano mai pass → cadono nella fascia `[1,9]` → **AI** (l'AI filtra il rumore). `negative_terms`/`negative_concepts` rimossi di default (recall: niente falsi negativi; il cliente filtra a valle). Il bonus `contact_available` (+4) si applica **solo se c'è già un segnale di nicchia** (un lead con solo telefono e zero keyword resta `no_match`, non spreca l'AI).
  - **Solo testo del profilo** è matchato (`username`, `full_name`, `biography`, `external_url`, `bio_links`). `scrape_source` (nome campagna/account) e `contact_fields` (cifre) NON sono matchati: una campagna chiamata "Shop survivor" faceva matchare "shop" su TUTTI i lead (`_lead_fields`). Tokenizzazione splitta anche `_` → keyword dentro gli handle (`@hanami_clothing`) matchano.
  - **Gate AI** (`classify_batch`): solo `status==ambiguous` E `score ∈ [ai_review_min, ai_review_max]`. Risultato tipico (627 lead, target moda): ~28% match deterministici, ~2% all'AI, resto no_match.
  - **Opzione `match_on_contact`** (filtro per-run, default False): se attiva, ogni lead con un contatto (telefono/email/whatsapp/link) → **match automatico** anche senza keyword (pagine super-in-target dove si contatta chiunque). Toggle in UI (pagina Qualifica lead). Sale a ~48% match, AI quasi azzerata.
  - **AI review rate-limit (free-tier Groq)**: la review è **serializzata** (semaphore=1) + **pacing** `AI_REVIEW_MIN_INTERVAL_SECONDS=8` per restare sotto il limite token/minuto (con 2 call parallele + retry si perdeva ~26% dei lead in `error`). Backoff `_classify_with_retry`: 6 tentativi, attese 5/10/20/40/60s. Prompt AI snellito (niente `RULES_JSON` ridondante). Test: `tests/test_lead_qualification_gating.py`.

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
| `gemini` | `AI_API_KEY=AIza... o AQ....` | `gemini-2.5-flash` | gratis, REST API propria; thinking disattivato (thinkingBudget=0). Groq free tier 70b = solo 100k token/giorno |

### Parametri chiave
- `AI_TEMPERATURE=0.35` — bassa per messaggi B2B consistenti (non alzare oltre 0.5)
- `AI_SYSTEM_PROMPT` — se vuoto, usa il default ottimizzato in `ai_personalizer.py:DEFAULT_SYSTEM_PROMPT`
- Il system prompt default: ruolo B2B, regole numerate per priorità, "preserva struttura template", "grammaticalmente corretto", "non inventare dalla bio"

### Flusso generazione
1. `generate_message()` → legge `settings.ai_provider` → branch sul provider
2. `_build_user_prompt()` → costruisce il prompt utente con template + bio + contesto campagna
3. `_get_system_prompt()` → usa `AI_SYSTEM_PROMPT` da .env oppure `DEFAULT_SYSTEM_PROMPT`
4. `_validate_message()` → strip virgolette, **preserva gli a-capo `\n`** (normalizza CRLF, collassa 3+ righe vuote), truncate, fallback. ⚠️ Gli a-capo NON sono più collassati: a send-time `_human_type` li batte come `Shift+Enter` (Enter da solo invierebbe il DM su IG web). Vedi flusso `send_dm`.

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
7. `_human_type()` → typing lognormale con pause tra parole; gli a-capo del messaggio vengono battuti come `Shift+Enter` (newline senza invio), tipando riga per riga
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
- **Resilienza blip rete/DB (tutti i worker)**: con `NullPool` ogni `db.execute` apre una nuova connessione asyncpg → un drop di rete verso il pooler Supabase (es. `OSError: [WinError 121]`, proxy/USB/WiFi caduto, pooler irraggiungibile) faceva fallire il job e fermava la campagna. Ora `message/bio/list/import_worker` nel `except Exception` chiamano `is_transient_db_error(e)` (`app/utils/db_resilience.py`: classifica `OSError`/`ConnectionError`/`asyncio.TimeoutError`/SQLAlchemy `InterfaceError`/`OperationalError`/`DBAPIError(connection_invalidated)`/errori conn asyncpg, risalendo la catena `__cause__`/`.orig`) → se transitorio `raise Retry(defer=60)` (il job riprende da solo al ritorno rete), altrimenti `raise` come prima (errori reali non mascherati). `database.py` aggiunge `timeout=15` al connect asyncpg (fail in ~15s invece di 60s). Tutti i task con `max_tries=10000` perché i retry non si esauriscano in un'outage prolungata.
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
- **Alert Telegram su stop scraping (2026-07-03)**: ogni `emit_event(action="scrape_stopped", level="error")` viene specchiato su Telegram da un hook in `app/utils/events.py` (fire-and-forget, `notifier.send_scrape_stop_alert`). Copre in un punto solo tutti gli stop errore di Fase Lista/Bio/import/challenge (rete giù, N fallimenti consecutivi, soft block, budget/pool, errori inattesi) — prima la fase scraping non aveva NESSUNA notifica Telegram (solo live log UI): gli alert esistevano solo nel percorso DM. I `level="warn"` (pausa globale, cap giornaliero) restano senza notifica: sono stati attesi/benigni. Test: `tests/test_scrape_stop_alerts.py`.
- **Warning Telegram early durante lo scraping (2026-07-03)**: `notifier.send_scrape_warning_alert(campaign_id, kind, detail)` avvisa MENTRE la run continua, per condizioni che meritano un intervento prima dello stop automatico: `soft_block` (ogni 429/soft-block, dal 1°) e `network_flaky` (≥3 errori di rete nella run anche se i retry recuperano, `NETWORK_FLAP_WARN_THRESHOLD` in `scrape_bios.py`). Throttle 15 min per (campagna, kind) nel notifier (`_warn_last_sent`). Il messaggio include bottone inline "Metti in pausa la campagna" (riusa la callback `pause:{id}` di telegram_commands) e hint `/halt`. Test: `tests/test_scrape_warning_alerts.py`.
- Problemi su singolo account non fermano tutto il bot: `cooldown`, `challenge_required` e `banned` isolano l'account; vengono pausate solo le campagne che non hanno altri account DM utilizzabili. Il kill-switch resta per problemi sistemici o comando manuale.

### Two-phase scraping: Fase Lista + Fase Bio (✅ IMPLEMENTATA)
Lo scraping `source_type='scrape'` è separato in due fasi indipendenti, ognuna avviabile/fermabile con target opzionale. Risolve i challenge "comportamento automatizzato" che colpivano l'estrazione lista su pagine grandi (9k+).
- **Fase Lista** (`app/services/scrape_list.py`, worker `list_followers_task`, stato `listing`/`listing_break`): pagina la lista follower/following a blocchetti `random(20,40)` passati come `max_amount` a `user_followers_v1_chunk`, crea `Follower(status=pending)` con sole info base (username, full_name, pic, flags). NON chiama `user_info_v1` → nessun consumo di cap. Delay lognormale 5-10s tra pagine + pausa lunga occasionale. Rispetta `campaign.list_target`. Endpoint: `POST /campaigns/{id}/list/start` (body `{target}`), `/list/stop`. **Rotazione account per-pagina**: con 2+ account scraping/both nel pool, ogni pagina è richiesta da un account diverso (`pool.next` round-robin nel loop) — il cursore `max_id` è lato-IG e funziona con qualunque account, quindi le richieste di lista si distribuiscono (~metà per account con 2) abbassando il footprint per-account. ⚠️ `list_page_size_min/max` (20-40) è inviato come `count` MA IG lo tratta come suggerimento e restituisce la sua pagina naturale (~50) per i follower: il 20-40 serve solo a garantire 1 sola richiesta IG per pausa (stando sotto ~50); non rende i blocchi più piccoli. La leva anti-detection reale resta il delay tra pagine. **Alzare il target su lista "completata" (fix 2026-06-13)**: lo stop per `list_target` raggiunto **conserva** `scrape_cursor` (solo l'esaurimento reale IG — `not batch`/`not max_id` — lo azzera, flag `ig_exhausted`), così rialzare il target riprende dalla posizione IG senza rescan. Il guard di `start_list` applica `body.target` PRIMA del check e usa l'helper puro `list_start_blocked(cursor, existing_count, list_target)`: blocca solo se davvero saturo (cursore None E `existing≥target`, o target None già drenato); target alzato sopra il count → restart permesso (con cursore già perso = una rescan-dedup, poi pulito). ⚠️ `_fetch_followers_chunk` (scraper.py) NON maschera più un throttle mid-paginazione come fine lista: ri-solleva l'eccezione se `max_id` è presente (fallback non-chunk solo alla prima pagina) → un 429 mid-lista va in `error` (cursore preservato, restartabile), non in falsa-completata.
- **Fase Bio** (`app/services/scrape_bios.py`, worker `scrape_bios_task`, stato `scraping`/`scraping_break`): cicla i `Follower(status=pending)`, per ognuno `user_info_v1` → bio+contatti → `bio_scraped`, sotto cap, con `bio_target`, session break, rotazione `ScrapingPool`. Riusa l'helper estratto `fetch_and_store_bio` (scraper.py) che ritorna `(outcome, account_used, error)` — l'account reale usato serve a isolare quello giusto su challenge con la rotazione round-robin. Endpoint: `POST /campaigns/{id}/bios/start` (body `{target}`), `/bios/stop`.
- `POST /campaigns/{id}/start-scrape` per `source_type='scrape'` ora instrada alla Fase Lista (imposta `listing` + `enqueue_list`); import resta `enqueue_resolve`. Il vecchio `scrape_followers_task` resta registrato per job in volo ma il nuovo flusso non lo accoda.
- **Progress**: `CampaignResponse.list_progress`/`bio_progress` (`{done, target}`) calcolati da `compute_phase_progress` (lista done = tutti i follower; bio done = `bio_scraped` + stati a valle). Frontend: `TwoPhasePanel` (due card) nel dettaglio campagna.
- **Pause sessione (resume via Retry(defer))**: lista e bio NON dormono in-job. Al raggiungimento di `scrape_session_size` impostano `*_break` + `scrape_break_until`, committano e `return seconds`; il worker (`list_followers_task`/`scrape_bios_task`) solleva `Retry(defer=seconds)` → arq ri-esegue lo stesso job dopo il defer (sopravvive a restart finché Redis persiste). Al rientro la funzione flippa `*_break`→attivo ed emette `scrape_resume`. ⚠️ **Micro-yield bio (fix 2026-06-13, `MICRO_YIELD_EVERY=100` / `MICRO_YIELD_MAX_SECONDS=40min` in `scrape_bios.py`)**: la sola pausa lunga NON bastava a stare sotto `job_timeout=3600`. Una sessione bio = `scrape_session_size` (250) lookup CON sleep `bio_fetch_delay` per-lead in-job: ~250×~18s ≈ 4700s > 3600s, quindi arq cancellava il job (`TimeoutError`/`CancelledError`) PRIMA di raggiungere il defer di pausa (la fase Lista non soffre: i batch sono veloci e raggiungono il break in secondi). Ora la bio cede ad arq ogni 100 bio (o 40 min wall-clock) con un `return 2` (defer brevissimo, **status resta `scraping`**, nessun `*_break`, nessun evento utente): il job successivo riprende subito dai pending → nessun job si avvicina al cap, senza un limite hard sul totale dei lead. La pausa lunga anti-block (30-45 min) resta separata e **ancorata a `done`** (count `bio_scraped` persistito, `next_long_break = ((done//size)+1)*size`) così i micro-yield, che azzerano i contatori locali a ogni restart, non la cancellano. `job_timeout=3600` resta come dead-man's-switch per un `user_info_v1` davvero appeso. ⚠️ Entrambi i task DEVONO essere registrati con `func(..., max_tries=10000)` in `WorkerSettings` — col default 5 una lista/bio lunga (decine di pause) verrebbe abortita da arq dopo 5 break. Durata pausa **lista hardcoded 2-5 min** (non usa `scrape_break_minutes_*` della campagna, che valgono solo per la bio = 30-45). `resume_scrape_break` ("Riprendi subito") per la bio chiama `enqueue_bios` (cancella la retry parcheggiata e ri-accoda subito); legacy `scraping_and_running` e import resolver fanno invece self-poll DB ogni 10s. NON aggiungere cron che riaccoda i `*_break` a timer: racerebbe con Retry(defer) → doppio job. **Guardia concorrenza enqueue (`_reenqueue_phase` in `work_enqueue.py`)**: gli enqueue scrape/list/bios NON cancellano più `arq:in-progress:{job_id}` (era il lock arq "1 job per id"): se il job è già in esecuzione l'enqueue esce no-op, altrimenti cancella solo `arq:job`+`arq:retry` e ri-accoda. Cancellare l'in-progress faceva partire un job bios duplicato concorrente → `ScrapingPool.build` non trovava slot liberi (li teneva il job vivo) → `ScrapingSlotsBusy` → campagna in `error` + arq `KeyError` su `job_tasks`. `ScrapingSlotsBusy(ScrapingPoolEmpty)` (slot tutti occupati, nessun login fallito) è transitorio: `scrape_bios` la cattura ed esce no-op SENZA mettere `error`.
- **Recovery**: `reenqueue_active_work` (solo su `/admin/resume` e Telegram `/unhalt`) riaccoda `listing`→list, `scraping`(scrape)→bios, `scraping_and_running`→legacy; `listing_break`→`listing`. `daily_reset` riavvia la Fase Bio messa in pausa per cap (`scrape_capped`) se restano `pending`. Challenge handler condiviso `is_challenge_exception`/`isolate_challenged_account` (scraper.py) isola solo l'account colpito. Migrazione `016_two_phase_scraping.py` (colonne `list_target`/`bio_target`). Piano: `docs/superpowers/plans/2026-06-09-two-phase-scraping.md`.

### Scraping avanzato + raccolta contatti (✅ IMPLEMENTATA)
- **Estrazione contatti in 1 call IG**: modulo puro `app/utils/contact_extract.py` legge campi business IG (`public_phone_number`, `public_email`, `whatsapp_number`, link profilo) + regex su bio (telefono, email, WhatsApp) e restituisce un `ContactData`. Consumato da scraper (`scraper.py`) e resolver import (`import_resolver.py`).
- **Lead visto a scrape-time**: ogni profilo scrapato/risolto viene fatto confluire in `global_contacts` via `app/services/global_contact_service.py` (`upsert_lead` + merge gap-fill). Il record diventa visibile nei lead anche prima che venga inviato qualsiasi DM (`last_contacted_at=NULL`). I campi contatto vengono arricchiti a send-time in `campaign_orchestrator._mark_globally_contacted`.
- **Messaggistica opzionale**: toggle `campaigns.messaging_enabled` — se False, la campagna termina come `completed` dopo lo scraping senza inviare DM. Guard in `/start` e `/start-dm-auto` (HTTP 400 se disattivata o template mancante). Frontend: toggle "Invia messaggi" nel form nuova campagna.
- **Cap anti-ban per-account**: `SCRAPE_DAILY_LIMIT` (env) + `campaigns.scrape_daily_limit` (override). Contatore `instagram_accounts.scrape_lookups_today` aggiornato dopo ogni `user_info` call; `has_scrape_budget`/`increment_scrape_lookup` in `account_manager.py`. Al raggiungimento del cap, lo scraper/resolver ruota su account alternativo o mette la campagna in pausa (`scrape_capped`). Contatore resettato dal cron `daily_reset`.
- **Export leads filtrabile**: endpoint `/leads` e `/leads/export` estesi con filtri `campaign_ids[]`, `scraping_account_ids[]`, `has_phone`, `has_email` — nessuna cross-client data leak. Frontend: multi-select filtri + colonne contatto nella pagina leads. **Filtro temporale su data scraping**: `date_from`/`date_to` filtrano `coalesce(first_seen_at, created_at)` (scrape-date, NON last_contacted — include lead solo-scrapati mai contattati); `date_to` bare-date è inclusivo (`< +1giorno`). Frontend `/leads`: select preset (Sempre/Oggi/Ieri/Ultimi 7gg/30gg/Questo mese/Personalizzato) che calcola il range, oltre ai date-picker custom. Colonna `first_seen_at` nel CSV.
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
