# OVERVIEW — BOT OUTBOUND

Cos'è il progetto, con quale stack è costruito e dove sta ogni file. Estratto da `CLAUDE.md` il 2026-07-29 (contenuto invariato).

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
│   │   │   ├── campaign_control.py   # Controlli condivisi pausa/ripresa campagna (web + Telegram)
│   │   │   ├── scrape_inbox_browser.py  # Fase Lista via browser (dm_threads, inbox_engine=browser): motore separato da scrape_inbox.py, stesso contratto di ritorno
│   │   │   └── inbox_browser/        # Moduli di supporto del motore inbox browser (funzioni pure + DOM)
│   │   │       ├── targa.py          # Targa provvisoria negativa (SHA-256 username normalizzato) + e_provvisoria
│   │   │       ├── testo.py          # Parsing testo/lingua inbox web (IT/EN), tri-stato ultimo_nostro
│   │   │       ├── riconoscimento.py # ArchivioNomi + contatore di zona: il riconoscimento decide solo il ritmo, mai se aprire
│   │   │       ├── ritmo.py          # Pause lognormali troncate per riestrazione, differenziate per zona piena/rapida
│   │   │       ├── pagina.py         # Interazione DOM: scroll sotto il buffer virtualizzato, apertura riga per contenuto, fondo/lento/piantato
│   │   │       ├── salvataggio.py    # Dedup in scrittura per username, fusione con precedenza di stato
│   │   │       └── gate.py           # Vincolo di configurazione: inbox_engine=browser richiede enrichment+bio_engine browser
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
├── CLAUDE.md                         # Panoramica + rimandi (vedi INDEX.md)
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

## Convenzioni codice

- **Async everywhere**: tutti i servizi, worker e route handler sono `async def`
- **Dependency injection**: usare `Depends(get_db)` per le sessioni DB, mai creare sessioni manualmente nelle route
- **Errors**: sollevare `HTTPException` nelle route, eccezioni custom in `utils/exceptions.py` nei servizi
- **No ORM lazy loading**: usare `selectinload` o `joinedload` esplicitamente dove necessario
- **Logging**: usare `loguru` — mai `print()` in produzione
- **Secrets**: mai loggare password, session_data o il SECRET_KEY

---

Vedi anche: [DATABASE.md](DATABASE.md) · [AI_ARCHITECTURE.md](AI_ARCHITECTURE.md) · [BROWSER.md](BROWSER.md) · [SCALA_E_PARALLELISMO.md](SCALA_E_PARALLELISMO.md) · [PRINCIPI_ANTI_DETECTION.md](PRINCIPI_ANTI_DETECTION.md) · [../setup/CONFIGURAZIONE.md](../setup/CONFIGURAZIONE.md)
