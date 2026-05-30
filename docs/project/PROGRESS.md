# PROGRESS — BOT OUTBOUND

Registro cronologico di tutto ciò che viene implementato.

---

## [2026-04-14] Fase 1 — Foundation + Core API ✅ COMPLETATA

### Inizializzazione progetto
- Python 3.13 scelto (non 3.14 — troppo recente per instagrapi)
- Venv creato in `backend/venv/`
- ARQ scelto su Celery per compatibilità Windows e architettura async

### File creati
| File | Descrizione |
|---|---|
| `backend/pyproject.toml` | Metadati progetto Python |
| `backend/requirements.txt` | Dipendenze pip |
| `backend/alembic.ini` | Configurazione Alembic |
| `backend/alembic/env.py` | Env async per Alembic |
| `.env` | Variabili d'ambiente (SECRET_KEY Fernet auto-generata) |
| `.env.example` | Template senza secrets |
| `.gitignore` | Esclude venv, .env, data/, node_modules |
| `CLAUDE.md` | Documentazione architetturale completa |
| `PROGRESS.md` | Questo file |
| `docker-compose.yml` | Redis su porta 6379 |
| `start.bat` | Script avvio Windows (4 processi in finestre separate) |

### Backend Foundation
| File | Contenuto |
|---|---|
| `backend/app/config.py` | Pydantic Settings, legge `../.env` e `.env` |
| `backend/app/database.py` | Async SQLAlchemy engine, WAL mode, Base, get_db, create_tables |
| `backend/app/main.py` | FastAPI app, CORS, lifespan (crea tabelle al boot) |

### Modelli ORM
| File | Modello | Note chiave |
|---|---|---|
| `app/models/account.py` | `InstagramAccount` | 6 stati: active/warming_up/cooldown/banned/challenge_required/disabled |
| `app/models/campaign.py` | `Campaign` | 7 stati: draft→scraping→ready→running→paused→completed/error |
| `app/models/follower.py` | `Follower` | 7 stati, unique(campaign_id, ig_user_id) |
| `app/models/message.py` | `Message` | 4 stati: pending/sent/failed/retry |
| `app/models/activity_log.py` | `ActivityLog` | Audit trail |
| `app/models/global_contact.py` | `GlobalContact` | Deduplicazione cross-campaign |

### Schemas Pydantic
`account.py`, `campaign.py`, `follower.py`, `message.py`, `dashboard.py`

### Utils
| File | Funzione |
|---|---|
| `app/utils/crypto.py` | Fernet encrypt/decrypt per password account |
| `app/utils/timing.py` | Generatori delay log-normali (anti-detection) |
| `app/utils/exceptions.py` | Gerarchia eccezioni custom |
| `app/utils/retry.py` | Decorator async con exponential backoff |

### API Routers
| Router | Endpoints chiave |
|---|---|
| `api/accounts.py` | CRUD + verify-challenge |
| `api/campaigns.py` | CRUD + start-scrape/start/pause/resume/stop |
| `api/followers.py` | Lista paginata + skip + regenerate |
| `api/messages.py` | Lista + retry |
| `api/dashboard.py` | Stats, activity feed, timeline |
| `api/health.py` | Health check Ollama + Redis + DB |

**Verifica**: Backend avviato su porta 8000, DB creato automaticamente, tutti gli import OK ✅

---

## [2026-04-14] Fase 2 — Scraping Follower ✅ COMPLETATA

### Servizio Scraper
| File | Contenuto |
|---|---|
| `app/services/scraper.py` | instagrapi: login con session restore, scraping paginato, fetch bio, gestione errori |
| `app/workers/scrape_worker.py` | ARQ task: `scrape_followers_task` |
| `app/workers/message_worker.py` | ARQ task: `run_campaign_task` |
| `app/workers/task_queue.py` | ARQ WorkerSettings + cron daily_reset |

**Note tecniche**:
- Session data salvata su DB dopo ogni login (evita re-login)
- Fallback a `user_followers_v1_chunk` se API standard restituisce solo 249 follower
- Delay random 5-15s tra chiamate API (anti rate-limit)
- Pausa extra 30-60s ogni 200 follower

---

## [2026-04-14] Fase 3 — AI Personalizzazione Messaggi ✅ COMPLETATA

| File | Contenuto |
|---|---|
| `app/services/ai_personalizer.py` | httpx async → Ollama `/api/generate`, validazione output, fallback, batch generation |

**Prompt**: System prompt dettagliato che istruisce il modello su tono, lunghezza, naturalezza  
**Validazione**: Rigetta messaggi <20 char, >500 char, con placeholder `{...}` non sostituiti  
**Fallback**: Se bio vuota → template semplice con sostituzione nome  
**Retry**: 3 tentativi con exponential backoff via `@async_retry`

---

## [2026-04-14] Fase 4 — Engine Invio DM (Browser Layer) ✅ COMPLETATA

| File | Contenuto |
|---|---|
| `app/services/account_manager.py` | Rotazione account, warm-up progressivo, cooldown escalation, record success/failure |
| `app/services/human_behavior.py` | SessionManager: limiti sessione, break obbligatori, finestra oraria, distraction pauses |
| `app/services/campaign_orchestrator.py` | Loop principale campagna: state machine, deduplicazione global_contacts, gestione errori IG |
| `app/services/dm_sender.py` | Bridge orchestrator ↔ Patchright, graceful fallback se Patchright non installato |
| `app/browser/context_manager.py` | Pool browser Patchright, profili persistenti per account, canvas noise injection |
| `app/browser/instagram_page.py` | Page Object Model: login, navigate, send_dm, human typing, simulate browsing |
| `app/browser/fingerprint.py` | Fingerprint deterministico per account (viewport, user-agent, locale, timezone) |

**Note anti-detection**:
- Profilo Chromium persistente per account (no incognito!)
- Fingerprint stabile e unico per account (derivato da account_id)
- Typing umano: 80-200ms/char + pause occasionali + typo rarissimi
- Browse profile 5-30s prima di aprire DM
- Sessioni: 10-20 DM poi pausa 30-60min
- Cooldown escalation: 30min → 2h → 12h

**Installazione Patchright** (da fare prima dell'uso):
```
cd backend
venv\Scripts\activate
pip install patchright
patchright install chromium
```

---

## [2026-04-14] Fase 5 — Frontend Next.js ✅ COMPLETATA

**Stack**: Next.js 14 (App Router) + TypeScript + Tailwind + shadcn/ui (Base UI) + SWR

| Pagina | File | Contenuto |
|---|---|---|
| Dashboard | `app/page.tsx` | Stats grid, activity feed con icone |
| Campagne | `app/campaigns/page.tsx` | Lista con progress bar, azioni start/pause/stop |
| Nuova campagna | `app/campaigns/new/page.tsx` | Form con template + contesto AI |
| Dettaglio campagna | `app/campaigns/[id]/page.tsx` | Progress, stats, lista follower |
| Account IG | `app/accounts/page.tsx` | Lista account, health badge, form aggiunta, challenge handler |
| Messaggi | `app/messages/page.tsx` | Log con filtri, retry button |
| Impostazioni | `app/settings/page.tsx` | Health check sistema, guida setup |

**Utility**: `lib/api.ts` (wrapper fetch → backend), `lib/types.ts` (TypeScript types), `lib/dateUtils.ts`

**Verifica**: `npx tsc --noEmit` → 0 errori ✅

---

---

## [2026-04-15] Sessione di fix, ottimizzazioni e nuove feature

### Bug fix critici (backend)

| Fix | File | Dettaglio |
|---|---|---|
| 429 su scraping target | `scraper.py` | `user_info_by_username` → `user_info_by_username_v1` (usa endpoint privato autenticato, non pubblico) |
| Strict mode violation browser | `instagram_page.py` | `:has-text("Message")` → `:text-is("Message")` per match esatto (evitava match su "Messages - N notification" navbar) |
| Message button non cliccabile | `instagram_page.py` | Aggiunto `window.scrollTo(0,0)` dopo il browsing del profilo prima di cliccare il pulsante |
| DM input non trovato | `instagram_page.py` | Aggiunto `wait_for_url('/direct/')` post-click + timeout 20s + popup dismiss più robusto |
| Contatori falliti non aggiornati | `campaign_orchestrator.py` | `except Exception` ora incrementa `message.retry_count` e dopo 3 retry segna follower `failed` + aggiorna `messages_failed` |
| Worker crash su eccezione | `campaign_orchestrator.py` | `await db.commit()` wrappato in try/except con rollback per evitare propagazione eccezione secondaria |

### Frontend — Dashboard completa

| Feature | Dettaglio |
|---|---|
| Fix timezone +2h | `dateUtils.ts`: `parseUTC()` aggiunge `Z` alle datetime UTC senza suffisso |
| Health strip | Indicatori colorati Database/Redis/Ollama con badge globale |
| Timeline chart | Bar chart CSS puro, 24 ore, dati da `/dashboard/timeline` |
| Account overview | Stacked bar orizzontale per stati account |
| Campagne attive | Widget campagne running/paused/scraping con progress bar |
| Account edit dialog | Dialog modifica proxy/limite/note + dropdown stato |
| Account disable/enable | Bottone rapido su ogni card |
| Follower skip | Bottone per saltare follower pending/bio_scraped/message_generated |
| Follower regenerate | Bottone per rigenerare messaggio AI per follower |
| Pagina `/guide` | Guida completa 11 sezioni da GUIDA.md, con indice cliccabile |
| Sidebar aggiornata | Voce "Guida" aggiunta |
| Settings snellita | Mini-guida rimossa, link a `/guide` |

### Ottimizzazioni anti-detection

| Miglioramento | File | Dettaglio |
|---|---|---|
| Scrolling randomizzato | `instagram_page.py` | 4 tipi di azione: scroll piccolo (touchpad), scroll grande, pausa lettura, hover mouse |
| Typing lognormale | `instagram_page.py` | Delay lognormale per tasto, pause tra parole (15% prob), micro-pause intra-parola |
| Timing più variato | `timing.py` | sigma lognormale alzato a 0.7, distraction pause lognormale, pre_dm_browse lognormale |
| Ordine follower random | `campaign_orchestrator.py` | `ORDER BY func.random()` invece di `created_at` |

### Lead database

| Feature | File | Dettaglio |
|---|---|---|
| Modello espanso | `global_contact.py` | +`username`, `full_name`, `biography`, `contact_history` (JSON array) |
| Migrazione inline | `database.py` | `ALTER TABLE ADD COLUMN` al boot con try/except, sicura su DB esistente |
| Salvataggio dati lead | `campaign_orchestrator.py` | `_mark_globally_contacted` ora salva username/bio/account che ha inviato/campagna |

### Configurazione test aggressivo

`.env` attuali (per testing — ripristinare per produzione):
```
MIN_DELAY_SECONDS=10
MAX_DELAY_SECONDS=45
SESSION_MIN_MESSAGES=5
SESSION_MAX_MESSAGES=12
SESSION_BREAK_MIN_MINUTES=10
SESSION_BREAK_MAX_MINUTES=25
```

Valori produzione raccomandati:
```
MIN_DELAY_SECONDS=120
MAX_DELAY_SECONDS=480
SESSION_MIN_MESSAGES=10
SESSION_MAX_MESSAGES=20
SESSION_BREAK_MIN_MINUTES=30
SESSION_BREAK_MAX_MINUTES=60
```

---

---

## [2026-04-15] Feature multi-account + fix bug audit ✅

### Bug fix da AUDIT.md (tutti 10 risolti)

Vedi dettagli completi nell'AUDIT.md. Tutti i bug BUG-01…BUG-10 risolti.

### Feature: Multi-account parallelo per campagna

**Architettura implementata**: SQLite WAL optimistic locking — nessuna Redis queue aggiuntiva necessaria.

| File | Modifica |
|---|---|
| `app/models/campaign_account.py` | **NUOVO** — CampaignAccount: join table campaigns ↔ accounts con `daily_limit_override`, `is_active` |
| `app/models/campaign.py` | +`daily_limit` (limite DM/giorno per l'intera campagna) |
| `app/models/follower.py` | +`locked_by_account_id`, `locked_at` (ottimistic locking) |
| `app/schemas/campaign_account.py` | **NUOVO** — CampaignAccountAssign, CampaignAccountUpdate, CampaignAccountResponse |
| `app/schemas/campaign.py` | +`daily_limit` in Create/Update/Response |
| `app/api/campaign_accounts.py` | **NUOVO** — CRUD account per campagna: list/assign/update/unassign |
| `app/api/campaigns.py` | `start` richiede ≥1 account attivo; `_enqueue_campaign_run` avvia 1 ARQ task per account |
| `app/services/campaign_orchestrator.py` | **RISCRITTO** — worker per singolo account, claiming atomico, crash recovery, limiti gerarchici |
| `app/workers/message_worker.py` | Nuova firma: `run_campaign_task(ctx, campaign_id, account_id)` |
| `app/workers/task_queue.py` | `daily_reset` riavvia worker per campagne running; nuovo cron `release_stale_locks` ogni 15min |
| `app/database.py` | 3 nuove migrazioni inline: `daily_limit`, `locked_by_account_id`, `locked_at` |
| `app/main.py` | Registra router `campaign_accounts` |

**Funzionalità garantite**:
- Parallelismo reale: N account → N ARQ job indipendenti
- No doppio contatto: `UPDATE WHERE locked_by_account_id IS NULL` atomico in WAL mode
- Crash recovery: lock stale (>20min) rilasciati da cron e ad ogni claim attempt
- Limiti gerarchici: `daily_limit_override` per campagna > warmup-adjusted account limit
- Limit campagna live: `COUNT(messages WHERE sent_at >= UTC midnight)` — nessun contatore stale
- Self-exit + midnight restart: worker esce al limite, cron lo riavvia all'alba

### Frontend multi-account

| File | Modifica |
|---|---|
| `lib/types.ts` | +`CampaignAccount`, `CampaignAccountAssign`, `CampaignAccountUpdate`; +`daily_limit` su Campaign |
| `lib/api.ts` | Aggiunto namespace `campaignAccounts` con list/assign/update/unassign |
| `campaigns/new/page.tsx` | +campo `daily_limit` opzionale |
| `campaigns/[id]/page.tsx` | Sezione "Account assegnati": assign/unassign/edit limit override + warning no account |

### 6 bug aggiuntivi trovati e risolti durante check finale

| # | File | Bug | Fix |
|---|---|---|---|
| FIX-A | `campaign_orchestrator.py` | Import inutilizzato `NoAvailableAccountError` | Rimosso |
| FIX-B | `campaign_orchestrator.py` | `date.today()` usa timezone locale invece di UTC per calcolo daily sent | `datetime.utcnow().replace(hour=0, ...)` |
| FIX-C | `campaigns.py` | `if data.daily_limit is not None:` impediva di azzerare il limite (set to null) | `if "daily_limit" in data.model_fields_set:` |
| FIX-D | `campaigns.py` | Status check in `update_campaign` bloccava modifica `daily_limit` su campagna running | `daily_limit` ora aggiornabile in qualsiasi stato; altri campi restano protetti |
| FIX-E | `campaigns.py` | `reset_campaign` non azzerava i lock sui follower | `.values(status=bio_scraped, locked_by_account_id=None, locked_at=None)` |
| FIX-F | `campaigns/[id]/page.tsx` | Import inutilizzato `formatDateTime` → TypeScript warning | Rimosso |

---

## [2026-04-15] Audit esterno completo → AUDIT.md

Vedi `AUDIT.md` per il report completo. Sintesi:

### Bug critici trovati (da fixare in ordine)

| # | Severità | Bug | File |
|---|---|---|---|
| BUG-01 | **CRITICO** | Patchright mancante → `return` silenzioso → false "sent" + global_contacts avvelenati | `dm_sender.py:31` |
| BUG-02 | **ALTO** | Retry non resetta `follower.status` → retry non funziona per follower `failed` | `messages.py:48` |
| BUG-03 | **ALTO** | Reset campagna non pulisce follower → campagna completa istantaneamente al restart | `campaigns.py:175` |
| BUG-04 | **ALTO** | `messages_pending` non inizializzato dallo scraper → sempre 0 in UI | `scraper.py:87` |
| BUG-05 | **MEDIO** | `consecutive_failures` cross-account → cooldown prematuro su account sano | `campaign_orchestrator.py:44` |
| BUG-06 | **MEDIO** | `secret_key` default `""` non validato a startup | `config.py:16` |
| BUG-07 | **MEDIO** | `hardware_concurrency`/`device_memory` calcolati ma non iniettati nel browser | `context_manager.py` |
| BUG-08 | **MEDIO** | User Agent stale: Chrome 121-124 (attuale: 136) | `fingerprint.py:18` |
| BUG-09 | **BASSO** | Migration swallow tutte le eccezioni silenziosamente | `database.py:44` |
| BUG-10 | **BASSO** | Canvas noise manca `toDataURL`/`toBlob` | `context_manager.py:68` |

### Sicurezza
- Nessuna autenticazione API (accettabile localhost, critico per deploy remoto)
- `session_data` correttamente NON esposto in API response ✅
- `secret_key` default vuoto — fix richiesto

### Compliance
- **GDPR**: scraping dati personali EU senza consenso → violazione strutturale
- **Instagram ToS**: violazione intrinseca al prodotto
- **Spam law IT**: DM commerciali non sollecitati

---

## [2026-04-16] Audit completo v2

### Scope
Analisi statica completa dell'intero codebase: backend (48 file Python), frontend (10+ pagine/componenti), database (7 tabelle), configurazione.

### Risultati

| Categoria | Conteggio |
|---|---|
| Bug critici | 4 |
| Bug alti | 8 |
| Bug medi | 12 |
| Bug bassi | 9 |
| Feature parziali | 5 |
| Rischi futuri | 7 |
| Miglioramenti proposti | 15 |

### Bug critici trovati
1. **BUG-NEW-01**: Race condition deduplicazione cross-campagna (global_contacts check-then-act non atomico)
2. **BUG-NEW-02**: Scraper ignora pause/stop utente durante esecuzione
3. **BUG-NEW-03**: Nessun indice su 5 colonne critiche usate in hot-path
4. **BUG-NEW-04**: Race condition TOCTOU in campaign completion tra worker concorrenti

### File creati/aggiornati
- `AUDIT.md` — sovrascritto con audit v2
- `FUTURE_IMPROVEMENTS.md` — **NUOVO** — 15 miglioramenti organizzati per priorita
- `INDEX.md` — aggiornata sezione bug aperti e file descriptions
- `PROGRESS.md` — aggiunta entry audit v2

### Verifica audit v1
Tutti i 10 bug dell'audit v1 (2026-04-15) confermati risolti nel codice attuale.

---

## [2026-04-16] Sessione fix operativi + nuove feature

### Feature aggiunte

| Feature | File | Dettaglio |
|---|---|---|
| Login browser manuale | `services/manual_login.py` **NUOVO** | Apre browser reale per login manuale — nessun API automated login, zero rischio ban IP |
| Endpoint `/manual-login` | `api/accounts.py` | POST che avvia `manual_browser_login_sync`, salva cookies come sessione instagrapi |
| Endpoint `/dm-count` | `api/accounts.py` | GET che restituisce conteggio DM non letti + richieste pending |
| Endpoint `/metrics` | `api/accounts.py` | GET metriche account: today_sent, success_rate, ban_events, challenge_events |
| Endpoint `/force-cancel-cooldown` | `api/accounts.py` | POST per forzare la cancellazione del cooldown di un account |
| Endpoint `/requeue` | `api/followers.py` | POST per rimettere in coda follower falliti/saltati — reset a `bio_scraped`, delete message, clear lock |
| Reply checker | `services/reply_checker.py` **NUOVO** | Cron ogni 30 min, scansiona inbox DM, marca follower come `replied` |
| Pagina Lead | `frontend/app/leads/page.tsx` **NUOVO** | GlobalContact con filtri, storico campagne, export CSV |
| A/B testing (M10) | `campaign.py`, `ai_personalizer.py`, `campaign_orchestrator.py` | `message_template_b` opzionale, 50/50 random variant assignment, `/ab-stats` endpoint |
| Pre-genera batch (M14) | `ai_personalizer.py`, `task_queue.py`, `campaigns.py` | `pre_generate_messages_task` via ARQ, endpoint `/pre-generate` |
| Approval queue (M15) | `ai_personalizer.py`, `campaigns.py` | `require_approval` + `approval_sample_size` su campaign, endpoints `/approval-queue`, `/approve-message`, `/reject-message` |
| Live worker log | `utils/events.py` **NUOVO**, `campaigns.py` | `emit()` via Redis, endpoint `/events`, frontend live log panel con polling |
| Browser profile cleanup | `api/accounts.py` | Delete cancella anche profilo Chromium su disco (BUG-NEW-15) |

### Bug fix

| Fix | File | Dettaglio |
|---|---|---|
| Message button sbagliato | `instagram_page.py` | Scoped selettori a `header` — evita match su widget "Messages" in basso a destra |
| Selettori lenti | `instagram_page.py` | `.or_()` combinators — 3s fallback vs 20s+ sequenziale |
| Three dots fallback | `instagram_page.py` | Menu ⋯ → "Invia messaggio" / "Send message" con selettori EN/IT completi |
| Worker log riappare dopo clear | `campaigns/[id]/page.tsx` | Non resettare `lastEventIdRef.current` — solo clear display |
| Cooldown force-cancel bug | `api/accounts.py` | Leggeva `cooldown_until` DOPO averlo azzerato — salvato in variabile prima |

### Frontend

| Feature | File | Dettaglio |
|---|---|---|
| Force-cancel cooldown button | `accounts/page.tsx` | Bottone giallo RefreshCw su account in cooldown |
| Requeue button su follower | `campaigns/[id]/page.tsx` | RotateCcw su follower failed/skipped |
| Pre-genera forced refresh | `campaigns/[id]/page.tsx` | setTimeout per mutate follower/approval dopo pre-gen |

---

## [2026-04-16] Sessione migliorie complessive ✅

### Bug fix critici

| Fix | File | Dettaglio |
|---|---|---|
| **Browser overlap** (CRITICO) | `context_manager.py`, `campaigns.py`, `task_queue.py` | Più browser si aprivano simultaneamente → about:blank + PC surriscaldato. Fix: (1) `_job_id` su `enqueue_job` per deduplicazione ARQ, (2) mutex asyncio `_account_locks` per-account in `get_browser_context` |
| **Campagne fantasma dopo riavvio** | `main.py` | Campagne restavano `running` dopo chiusura terminali. Fix: `_auto_pause_orphaned_campaigns()` nel lifespan — al boot tutte le running → paused + release lock follower |
| **AI virgolette** | `ai_personalizer.py` | Ollama wrappava messaggi in `"..."`, `«...»`. Fix: strip in `_validate_message` + regola `NON mettere virgolette` nel SYSTEM_PROMPT |

### Performance

| Miglioramento | File | Dettaglio |
|---|---|---|
| Optimistic UI update | `campaigns/[id]/page.tsx` | `action()` usa risposta POST direttamente (`mutateCampaign(updated, false)`) — elimina secondo roundtrip GET |
| Redis timeout ridotto | `campaigns.py` | `_check_redis_reachable` 3s → 1s |
| DB indexes su modelli | `follower.py`, `message.py` | `index=True` su `campaign_id`, `sent_at` per nuovi DB |

### Osservabilità

| Feature | File | Dettaglio |
|---|---|---|
| Pre-genera eventi live | `task_queue.py`, `ai_personalizer.py` | `emit()` per start/progress/complete/error di pre-generazione — visibile in live log |
| Approval sampling eventi | `ai_personalizer.py` | `emit()` quando follower messi in pending_approval |
| Enqueue error eventi | `campaigns.py` | `emit()` se enqueue pre-gen fallisce |
| Worker error eventi | `campaign_orchestrator.py` | `emit_event` nel blocco `except Exception` — errori inattesi visibili in live log |
| Event colors frontend | `campaigns/[id]/page.tsx` | Colori per `pregen_*`, `approval_sampling`, `worker_error` |
| Reply checker logging | `task_queue.py` | `check_replies` logga sempre start + risultato (trovato/non trovato/errore) |

### DM count fix

| Fix | File | Dettaglio |
|---|---|---|
| Sessione stale | `api/accounts.py` | `/dm-count` ora usa `_login()` dal scraper (stessa logica del reply checker) — sessione refreshata, non stale |

---

## [2026-04-17] Sessione fix UX + pre-gen redesign ✅

### Bug fix

| Fix | File | Root cause / Dettaglio |
|---|---|---|
| **AI virgolette asimmetriche** | `ai_personalizer.py` | Ollama spesso mette solo `"` iniziale senza chiusura → check coppia non scattava. Fix: (1) loop strip coppie simmetriche (3 iter) + (2) strip virgoletta iniziale solitaria. Aggiunti U+2018/U+2019 (smart single quotes) |
| **Conteggio messaggi falliti stale** | `api/campaigns.py` | `messages_failed` era counter denormalizzato con drift. Fix: `_enrich_campaign` ora fa GROUP BY su `Follower.status` live — counters sempre coerenti con DB reale |
| **Paginazione follower lenta** | `api/followers.py`, `database.py` | COUNT usava subquery costosa + mancavano indici composti. Fix: count diretto + indici `idx_followers_campaign_updated`, `idx_followers_status`. Frontend: `keepPreviousData: true` (lista non scompare al cambio pagina) |

### Nuove feature

| Feature | File | Dettaglio |
|---|---|---|
| **Pre-genera preview-first** | `ai_personalizer.py`, `task_queue.py`, `api/campaigns.py`, `campaigns/[id]/page.tsx` | Nuovo flusso: Pre-genera → genera solo N campione (5s-30s) → box anteprima con 3 pulsanti. "Approva tutti": messaggi campione restano validi + parte batch completo. "Rigenera": cancella campione + nuovo pre-gen (per testare prompt modificato). "Modifica prompt": apre dialog |
| **Filtri follower per status** | `api/followers.py`, `campaigns/[id]/page.tsx` | Dropdown filtro: Tutti / In coda / Bio scraped / Msg creato / In approvazione / Inviati / Risposto / Falliti / Skippati |
| **Stat "Skippati"** | `schemas/campaign.py`, `api/campaigns.py`, `campaigns/[id]/page.tsx` | Nuovo campo `messages_skipped` nella stat bar campagna |
| **Template edit in stato paused** | `api/campaigns.py` | Permettere modifica prompt quando campagna è in pausa (workflow: pausa → modifica → rigenera anteprima) |
| **Session pre-check manual login** | `api/accounts.py`, `accounts/page.tsx` | Prima di aprire browser, verifica sessione via instagrapi (2-3s). Se valida → toast "Sessione già attiva" senza aprire browser. Se scaduta → browser come sempre |
| **`full_batch_generate_task`** | `task_queue.py` | Nuovo ARQ task separato per full batch post-approvazione anteprima |

### Rimosso / semplificato

| Rimozione | File | Motivo |
|---|---|---|
| Approval per-messaggio (Approva / Rigenera singolo) | `campaigns/[id]/page.tsx` | Sostituito da approvazione globale |
| `_apply_approval_sampling` call in `generate_messages_batch` | `ai_personalizer.py` | Logica preview ora upfront, non a posteriori |
| Insight "Verificati" nella pagina Leads | `leads/page.tsx` | Inutile per l'utente |

---

## [2026-04-18] Sessione fix critici runtime ✅

### Bug fix — ARQ Resume bloccato (root cause: 3 chiavi Redis stale)

| Fix | File | Dettaglio |
|---|---|---|
| **Resume non avviava worker** | `api/campaigns.py` | Root cause: ARQ conserva `arq:job:{id}`, `arq:in-progress:{id}`, `arq:retry:{id}` in Redis per ore dopo la fine del processo. `enqueue_job` usa SETNX → silenziosamente fallisce se chiave esiste. Fix: `_enqueue_campaign_run` ora cancella tutte e 3 le chiavi prima di ogni `enqueue_job` |
| **Daily cron stesso fix** | `task_queue.py` | `daily_reset` cron ora cancella le stesse 3 chiavi prima del re-enqueue notturno |
| **`keep_result = 0`** | `task_queue.py` | `WorkerSettings.keep_result = 0` — chiavi `arq:job:*` non persistono dopo completion normale |

**Come testato**: verificato con `memurai-cli KEYS "arq:*worker*"` — trovate `arq:in-progress:*` con TTL ~5h. Cancellate manualmente + chiamata resume → 44→48 messaggi inviati confermato.

### Bug fix — Pausa non fermava il browser a raffica

| Fix | File | Dettaglio |
|---|---|---|
| **Re-check status dopo delay** | `campaign_orchestrator.py` | Worker controllava `campaign.status` solo all'inizio del loop. Se campagna veniva messa in pausa durante `wait_between_messages` (10-45s), il browser si apriva comunque. Fix: check esplicito dopo il delay — se non `running`, rilascia lock follower + reservation globale + return |
| **Session break interrompibile** | `human_behavior.py` | `take_session_break` dormiva 10-60 min ignorando pausa. Aggiunto `take_session_break_interruptible()`: sleep a chunk da 5s + check `campaign.status` ogni 5s — ritorna `False` se fermata |
| **Orchestrator usa nuovo metodo** | `campaign_orchestrator.py` | Sostituito `take_session_break()` con `take_session_break_interruptible()` — esce immediatamente se pausa |

### Bug fix — `daily_message_count` stale

| Fix | File | Dettaglio |
|---|---|---|
| **Limit check usa live query** | `campaign_orchestrator.py` | Aggiunto `_get_account_daily_sent(account_id, db)`: conta `Message.status=sent AND sent_at>=oggi_UTC` per account. Sostituisce `account.daily_message_count` nel check limite giornaliero — mai stale anche se cron midnight non ha girato |
| **Boot sync contatore** | `main.py` | `_sync_daily_message_counts()` nel lifespan: allinea `daily_message_count` di ogni account con il conteggio reale da DB. Corregge drift accumulato da sessioni precedenti |

**Spiegazione discordanza dashboard (53) vs account (98) vs campagna (70)**:
- Dashboard: `Message.sent_at >= oggi_UTC` — corretto
- Campagna: `Follower.status IN (sent, replied)` ever — **all-time, non oggi**
- Account: `daily_message_count` — era stale da ieri (cron midnight non girava se ARQ worker spento). Ora risolto.

### Bug fix — Redis false negative al lancio campagna

| Fix | File | Dettaglio |
|---|---|---|
| **Timeout Redis check** | `api/campaigns.py` | `_check_redis_reachable` usava `socket_connect_timeout=1s`. Con Memurai sotto carico timeout troppo stretto → falso "Redis non raggiungibile". Alzato a 3s |

### Bug fix — `record_success` race condition (2 campagne stesso account)

| Fix | File | Dettaglio |
|---|---|---|
| **Incremento atomico** | `services/account_manager.py` | `daily_message_count += 1` via ORM = read-modify-write, race con 2 worker concorrenti. Fix: `UPDATE SET daily_message_count = daily_message_count + 1` atomico in SQL |

### Feature — Stop/Resume ridisegnato

| Feature | File | Dettaglio |
|---|---|---|
| **Stop → paused (non completed)** | `api/campaigns.py` | `stop` ora imposta `status=paused` — follower e messaggi generati intatti, campagna riprendibile |
| **Resume da completed** | `api/campaigns.py` | `resume` ora accetta `paused` e `completed` — fix per campagne bloccate in stato completed con pending followers |
| **Reset da paused** | `api/campaigns.py` | `reset` ora accetta anche `paused` (oltre a error/completed/scraping) |
| **Confirm dialog su Stop** | `campaigns/[id]/page.tsx` | Stop button apre `openConfirm` con messaggio informativo ("dati non persi, puoi riprendere") |
| **Riprendi su completed con pending** | `campaigns/[id]/page.tsx` | Bottone "Riprendi" appare anche per `completed` con `messages_pending > 0` |
| **Reset visibile su paused** | `campaigns/[id]/page.tsx` | Reset button visibile anche per campagne in pausa |

### Note operative scoperte

- **Redis = Memurai** su Windows (non Docker), porta 6379. Ispezionabile con `D:/Memurai/memurai-cli.exe`
- **Due campagne stesso account**: funzionano in sequenza (mutex browser serializza), non in parallelo. Non raddoppia la velocità — raddoppia solo la coda. Per vera velocità parallela servono account distinti.
- **Limite giornaliero**: `daily_limit_override` per campagna-account è un limite specifico per quella campagna. Il limite totale account (`daily_message_count`) è condiviso tra tutte le campagne — la prima a esaurirlo blocca anche le altre.

---

## [2026-04-18] Sessione anti-detection + 7B Lite + BUG-NEW-32 ✅

### 7B Lite — Warning account già in uso

| File | Modifica |
|---|---|
| `api/campaign_accounts.py` | Check pre-assegnazione: se account è `is_active` in altra campagna `running/paused`, ritorna 409 `ACCOUNT_IN_USE:"nome_campagna"`. Query param `?force=true` bypassa il check. |
| `frontend/lib/api.ts` | `campaignAccounts.assign(id, data, force = false)` — aggiunge `?force=true` all'URL se richiesto. |
| `frontend/app/campaigns/[id]/page.tsx` | `handleAddAccount(force = false)`: intercetta 409 con prefisso `ACCOUNT_IN_USE:`, apre `ConfirmDialog` con spiegazione (browser serializzato, non parallelo), bottone "Assegna comunque" ri-chiama con `force=true`. |

### BUG-NEW-32 — send_dm: verifica navigazione + selettori DM-specific

| File | Modifica |
|---|---|
| `browser/instagram_page.py` | `navigated_to_direct = False` flag: distinto click-miss (URL non cambia) da input-non-trovato-dopo-navigazione. |
| `browser/instagram_page.py` | Selettori input DM ridotti a 4 DM-specific: `aria-label="Message"`, `aria-label="Messaggio"`, `placeholder="Message..."`, `placeholder="Messaggio..."`. Rimosso `div[contenteditable="true"]` (troppo generico — matchava elementi fuori dal thread DM). |
| `browser/instagram_page.py` | `found_selector` variabile: loggata con `logger.debug` al successo ("input trovato — selettore: ..."). |
| `browser/instagram_page.py` | Screenshot automatico `data/debug_no_input_{username}.png` quando nessun selettore trovato. |

### Typo system — _human_type

| File | Modifica |
|---|---|
| `browser/instagram_page.py` | `_QWERTY_ADJACENT: dict[str, str]` — mappa tasto → adiacenti, a livello modulo. |
| `browser/instagram_page.py` | `_typo_char(char)` — restituisce tasto adiacente casuale (preserva maiuscolo), None se non in mappa. |
| `browser/instagram_page.py` | In `_human_type`, per ogni char con `char_idx` (non primo/ultimo, in parola >3 lettere): ~8% prob → digita adiacente → pausa 150-500ms → Backspace → pausa → ridigita corretto. |

### SEC-05 — Validazione URL proxy nel form account

- `frontend/app/accounts/page.tsx`: regex `^https?://([^@]+@)?[^:]+:\d+$` applicata prima del submit. Toast errore con formato corretto se invalido. Hint testuale sotto il campo.

### Scraping slot — prevenzione doppio scraping stesso account

- `utils/instagrapi_client.py`: `_scraping_accounts: set[str]` + `_scraping_set_lock`. `acquire_scraping_slot` / `release_scraping_slot` / `get_scraping_account_ids` esportate.
- `scraper.py`: `_get_available_account` filtra account già in scraping. `scrape_followers` acquisisce slot post-selezione account, rilascia in `finally` — anche in caso di crash.

### S2 — Proxy scraping separato (SKIPPED)

Deciso non implementare. Utente usa hotspot mobile: stesso IP per scraping e DM è già diverso dall'IP fisso, quindi il rischio S2 non si applica.

---

## [2026-04-22] Sessione fingerprint diversificazione multi-account ✅

### Browser fixes (instagram_page.py)
- `_dismiss_ig_modals()` helper: unifica dismissal popup in metodo riutilizzabile, chiamato 3 volte (post-goto, post-browsing, post-navigazione DM). Usa `has-text` (non `text-is`) + `[role="button"]` fallback per sleep mode popup e simili.
- Selettori DM input: aggiunto `div[role="textbox"]` e `div[contenteditable="true"][role="textbox"]` — più robusto cross-locale. Rimossi `[placeholder="..."]` che non funzionano su `div[contenteditable]`.

### Fingerprint diversificazione per account (Opzione A anti-detection)

| File | Modifica |
|---|---|
| `browser/fingerprint.py` | +`WEBGL_PROFILES` (8 GPU reali ANGLE D3D11), +`TIMING_MULTIPLIERS` (0.80→1.30). Fingerprint ora include: `webgl_renderer`, `webgl_vendor`, `screen_width`, `screen_height`, `timing_multiplier`. Tutti deterministici da `account_id`. |
| `browser/context_manager.py` | `_build_fingerprint_script(fp)` sostituisce vecchio script. Nuovi override: `window.screen.*`, WebGL `RENDERER/VENDOR`, `AudioBuffer.getChannelData`, `measureText` font noise, Canvas `toBlob`. |
| `services/dm_sender.py` | Estrae `timing_multiplier` da fingerprint, passa a `InstagramPage`. |
| `browser/instagram_page.py` | `__init__(context, timing_multiplier=1.0)`. Browse time e typing base_ms scalati da `self._tm`. |

### Viewport fix
- `fingerprint.py`: altezze viewport ridotte di ~140px per non sforare sotto taskbar Windows quando `HEADLESS=false`.

### Backup
- Creato `d:\BOT OUTBOUND BACKUP V2\` (esclusi venv, node_modules, .next, db, screenshot).

---

## Da completare (Fase 6 — Hardening)

- [ ] Test unitari (timing, account_manager, ai_personalizer)
- [ ] Protocollo warm-up automatico avanzato
- [ ] Logging strutturato (loguru già incluso, configurare livelli)
- [ ] Migrazione Alembic (attualmente `create_all` + inline ALTER TABLE)
- [ ] Installare Patchright: `pip install patchright && patchright install chromium`
- [ ] Installare Ollama e scaricare modello: `ollama pull llama3.2`

---

## Roadmap futura (pianificata, non implementata)

### Fase 7 — Parallelismo e scala

**7A — Multi-account per campagna** ✅ **IMPLEMENTATA**
- Soluzione finale: SQLite WAL optimistic locking (no Redis queue aggiuntiva)
- 1 ARQ job per account assegnato — vero parallelismo
- Crash recovery via stale lock timeout (20min) + cron ogni 15min
- UI completa in `campaigns/[id]/page.tsx`

**7B — Multi-campagna parallela**
- Aggiungere `current_campaign_id` a `InstagramAccount` per assegnazione esplicita
- 1 account = 1 sola campagna alla volta (UI di assegnazione)
- ARQ `max_jobs` alzato per permettere N `run_campaign_task` in parallelo
- Complessità: Bassa (1 giorno)

**7C — Frontend per lead database** ✅ **IMPLEMENTATA**
- Pagina `/leads` con GlobalContact: username, bio, storico campagne/account
- Export CSV via `/leads/export`
- Filtri per campagna, data, follower, verified

### Fase 8 — Anti-detection avanzato

**Proxy per IP diversificazione** (necessario con 3+ account):

| Tipo | Trust IG | Costo/mese | Note |
|---|---|---|---|
| ISP proxy residenziale statico | ★★★★ | €2-5/IP | Buon compromesso |
| Mobile proxy 4G/5G | ★★★★★ | €30-80/IP | Ideale per alto volume |
| Dispositivi Android propri come proxy 4G | ★★★★★ | €5-10 app + SIM | Sfrutta hardware esistente |

**Dispositivi mobili come proxy** (raccomandato se si hanno Android disponibili):
- App: Proxidize Mobile o iProxy.online
- Il dispositivo diventa un proxy che instrada il traffico del bot sull'IP mobile 4G
- Nessuna riscrittura del bot — si configura solo il campo `proxy` nell'account
- Costo: ~€5-10/mese per SIM + app, zero costi hardware aggiuntivi
- Vedi sezione "Spiegazione proxy mobili" per dettagli

**Canvas/WebGL fingerprinting** — già parzialmente implementato in `fingerprint.py`, da migliorare con:
- Font randomization
- Audio context fingerprint noise

### Fase 9 — Automazione app nativa (lungo termine)

**Appium + dispositivi Android reali**:
- Automatizza la vera app Instagram (fingerprint identico utente reale)
- Costo: €30-80 per dispositivo Android economico
- Complessità: Alta (mesi di sviluppo, riscrittura layer browser)
- Vantaggio: anti-detection massimo, praticamente impossibile da distinguere

---

## [2026-05-09] Adversarial review kill-switch/resume - FIX COMPLETATI

### Obiettivo
Rendere il kill-switch globale realmente fail-closed: nessun DM deve partire dopo halt, lo scraping non deve risultare completato se interrotto dal kill-switch, e il resume deve rimettere in coda i job attivi senza alterare campagne pausate/completate.

### Fix critici applicati
- `BotHaltedError` aggiunta alla gerarchia eccezioni per distinguere halt globale da "nessun follower rimasto".
- `campaign_orchestrator._claim_next_follower()` ora solleva `BotHaltedError` se `BotState.halted=True`; il worker esce senza chiamare `_maybe_complete_campaign()`.
- Il worker DM ricontrolla il kill-switch dopo delay/browser setup e immediatamente prima di `message.status = sending`; se halted, rilascia lock follower + prenotazione `global_contacts` e non invia il DM.
- Il callback pre-send ora blocca anche un halt arrivato tra `sending` e pressione di Enter; `DMAbortedBeforeSendError` resetta il messaggio a `pending`.
- `scraper.py` propaga `BotHaltedError` da paginazione, batch bio-fetch e pausa sessione; il task salva i progressi parziali senza impostare `scrape_completed_at`, senza mettere `ready/running` e senza log `scrape_completed`.
- Soft block persistente nello scraper ora mette la campagna in `paused` invece di completare falsamente lo scraping.
- Nuovo `backend/app/services/work_enqueue.py`: helper condivisi per pulire chiavi ARQ stale e re-enqueue di scrape/DM worker.
- `/admin/resume` e Telegram `/resume` ora chiamano `reenqueue_active_work()` anche se il bot era gia' running, cosi' un secondo resume puo' riparare un precedente resume fallito per Redis down.
- Le campagne in `scraping_break` vengono ripristinate allo stato attivo precedente prima del re-enqueue.
- `daily_reset()` non riavvia worker DM se il kill-switch e' attivo e filtra solo account con ruolo `dm`/`both`.

### Fix hardening applicati
- `auth.py` non si fida piu' di `X-Forwarded-For` di default; nuovo `.env` `AUTH_TRUST_FORWARDED_FOR=false`.
- Mini-session recap Telegram resta disponibile ma e' configurabile con `TELEGRAM_SESSION_RECAP_ENABLED`.
- Screenshot Telegram su errori critici UI DM vengono inviati in background dopo la cattura, evitando di bloccare troppo il ramo errore.
- `api/campaigns.py` riusa gli helper ARQ condivisi per evitare drift tra start manuale e resume globale.

### File principali modificati in questo pass
- `backend/app/utils/exceptions.py`
- `backend/app/services/campaign_orchestrator.py`
- `backend/app/services/scraper.py`
- `backend/app/services/work_enqueue.py`
- `backend/app/api/admin.py`
- `backend/app/api/campaigns.py`
- `backend/app/services/telegram_commands.py`
- `backend/app/workers/task_queue.py`
- `backend/app/api/auth.py`
- `backend/app/config.py`
- `backend/app/browser/instagram_page.py`
- `.env.example`

### Verifica eseguita
- `python -m compileall backend\app` OK.
- `backend\venv\Scripts\python.exe -c "from app.main import app; from app.workers.task_queue import WorkerSettings; print('ok')"` OK.
- `npm run lint` in `frontend/` OK con 11 warning preesistenti non bloccanti.
- `backend/tests` non contiene test eseguibili.

### Note operative per prossimo LLM
- Il resume globale non cambia stati `paused`, `completed`, `error`, `draft`: riaccoda solo `running`, `scraping`, `scraping_and_running`, `scraping_break`.
- Se Redis e' down durante resume, un nuovo `/admin/resume` o Telegram `/resume` puo' essere ripetuto e riaccoda comunque perche' `reenqueue_active_work()` viene chiamato anche quando `BotState` non cambia.
- Non sono state toccate logiche proxy.

---

## [2026-05-11] Control-plane remoto multi-campagna + kill-switch web

### Obiettivo
Rendere i comandi a distanza adatti a piu' campagne attive: `/pause` e `/resume` non devono piu' agire globalmente o ambiguamente, ma devono far scegliere la campagna. Il kill-switch globale deve restare disponibile sul sito e via Telegram come comando separato di emergenza.

### Modifiche applicate
- Nuovo `backend/app/services/campaign_control.py`: helper condiviso per pausa/ripresa campagna, liste pausable/resumable, pre-check Redis e isolamento campagne senza account DM utilizzabili.
- `backend/app/api/campaigns.py` usa `campaign_control.py` per `POST /campaigns/{id}/pause` e `POST /campaigns/{id}/resume`, evitando drift tra web e Telegram.
- `backend/app/services/telegram_commands.py` riscritto con supporto `callback_query` e bottoni inline:
  - `/pause` mostra campagne attive e mette in pausa solo quella selezionata.
  - `/resume` mostra campagne in pausa e riprende solo quella selezionata.
  - `/halt [motivo]` attiva il kill-switch globale.
  - `/unhalt` disattiva il kill-switch globale e riaccoda i lavori ancora attivi.
- `backend/app/services/notifier.py` accetta `reply_markup` per inviare bottoni Telegram.
- `frontend/components/layout/Sidebar.tsx`: la sidebar admin ora mostra sempre il controllo kill-switch; **Blocca tutto** quando spento e **Sblocca** quando attivo.
- `backend/app/services/anomaly_detector.py`: un singolo `account_banned` non attiva piu' subito il kill-switch globale; il bot isola l'account e pausa solo campagne senza altri account DM utilizzabili. Il kill-switch resta per soglie sistemiche.
- `backend/app/services/campaign_orchestrator.py`: quando un account entra in cooldown per failure streak, vengono pausate solo le campagne rimaste senza account DM utilizzabili.
- `frontend/app/guide/page.tsx`, `GUIDA.md`, `CLAUDE.md`, `INDEX.md`: documentazione aggiornata.

### Comportamento atteso
- Piu' campagne possono restare attive contemporaneamente.
- Da Telegram, pausa/ripresa richiedono sempre una selezione campagna tramite bottone.
- Da web, il kill-switch globale e' esplicito e disponibile in entrambi gli stati.
- Problema singolo account: stop del worker/account coinvolto; altre campagne/account continuano se hanno risorse sane.
- Problema sistemico o comando `/halt`: blocco globale fail-closed.

### Verifica eseguita
- `backend\venv\Scripts\python.exe -m compileall backend\app` OK.
- `backend\venv\Scripts\python.exe -c "from app.main import app; from app.workers.task_queue import WorkerSettings; print('ok')"` OK.
- `npm run lint` in `frontend/` OK con 11 warning preesistenti non bloccanti.
- `npx tsc --noEmit` in `frontend/` OK.

---

## [2026-05-11] Fix login dashboard dopo passaggio Supabase

### Problema
La dashboard non accettava piu' le credenziali dopo il passaggio a Supabase.

### Root cause
- Il backend puntava correttamente a Supabase/Postgres, ma la tabella `users` sul nuovo DB era vuota: lo script di migrazione dati non aveva portato l'admin o non era stato eseguito per gli utenti.
- La prima verifica DB ha anche evidenziato `asyncpg.exceptions.DuplicatePreparedStatementError` con il pooler Supabase/PgBouncer: il pooler in modalita' transaction/statement non supporta prepared statements riusati.

### Fix applicati
- `backend/app/utils/db_dialect.py`: normalizzazione URL Postgres con `prepared_statement_cache_size=0`.
- `backend/app/database.py`: per Postgres usa `NullPool`, `statement_cache_size=0` e `prepared_statement_name_func` con nomi unici.
- `backend/alembic/env.py`: stessa configurazione pooler-safe per Alembic.
- Ricreato l'admin Supabase con `backend/scripts/create_admin.py` usando le credenziali locali indicate dall'utente in `data/profiles/PROFILI BOT.txt` (password non documentata).
- Aggiornati `SUPABASE_RLS.md` e `CLAUDE.md` con le note operative su Supabase Pooler e admin iniziale.

### Verifica eseguita
- Connessione Supabase OK dopo fix: query `users` riuscita.
- Stato utenti: `users total=1 active=1 admins=1`.
- Verifica password backend: OK.
- `backend\venv\Scripts\python.exe -m compileall backend\app backend\alembic` OK.
- Runtime import `from app.main import app; from app.workers.task_queue import WorkerSettings` OK.

### Nota operativa
Riavviare backend e worker ARQ dopo questo fix: la engine SQLAlchemy viene creata a import e un processo gia' avviato puo' mantenere la vecchia configurazione di connessione.

---

## [2026-05-22] Recovery DM riavvia il worker e riallineamento contesto

### Problema osservato
- `@primero_adv6` risultava `active`, sotto il limite DM e assegnato alla campagna `PRIMERO Outreach Rivenditori`, ma aveva smesso di inviare.
- Il DM verso `@sabinagypsy` era rimasto in `messages.status='sending'`; il recovery lo ha poi confermato `sent`, ma non esisteva piu' un worker DM attivo per quell'account.

### Root cause e fix
- Il recovery risolveva correttamente i messaggi `sending` stale ma non riparava il job account-specifico che poteva essere morto durante quell'invio.
- `backend/app/services/recovery_checker.py` ora riaccoda il worker dopo esito recovery (`recovered`, retry senza evidenza, giveup terminale) solo se:
  - la campagna e' ancora `running` o `scraping_and_running`;
  - l'account e' ancora `active` o `warming_up`;
  - la `campaign_account` e' ancora attiva con ruolo `dm` o `both`.
- Aggiunto test mirato in `backend/tests/test_recovery_checker.py` per il guardrail di ripartenza.
- Riaccodato manualmente il worker reale di `@primero_adv6` e verificato dagli eventi Redis che e' ripartito sul DM successivo.

### Contesto aggiornato
- `CLAUDE.md` ora esplicita che codice verificato e contesto vanno confrontati prima di modificare flussi esistenti: non si rimuovono guardrail/migliorie solo per seguire documenti indietro.
- `CLAUDE.md` e `INDEX.md` documentano worker DM short-lived, cron worker dedicato e recovery `sending` che riaccoda solo worker ancora validi.
- `INDEX.md` e questo log includono il cron worker nell'avvio manuale.

### Verifica
- `python -m py_compile app/services/recovery_checker.py tests/test_recovery_checker.py` OK.
- `python -m pytest tests/test_recovery_checker.py tests/test_operator_guardrails.py` OK (`7 passed`).
- `python -m pytest tests` ha eseguito `13 passed`; `tests/test_reservation.py` non si completa nel sandbox perche' usa il Postgres configurato. Non e' stata forzata la suite completa contro il DB operativo.

---

## [2026-05-23] Recap sessione rispettato, Stories viewer robusto e cancellazioni pre-send

### Problemi osservati
- `@primero_azienda_cbd` ha inviato un mini-session recap alle 14:04 con ripartenza prevista alle 14:36, ma ha ripreso alle 14:05.
- Root cause live: il `recovery_checker` ha visto un vecchio `sending` risolto e ha riaccodato subito il worker, senza controllare che ARQ avesse gia' un job/retry/in-progress differito per lo stesso account.
- Screenshot `backend/data/debug_no_input_nxsgrowshop2.png`: il browser era ancora nel viewer Storie Instagram, con input "Reply to...", quindi cercava l'input DM nel DOM sbagliato.
- Caso crash/PC spento: se il worker viene cancellato dopo lock follower/reservation ma prima di `message.status='sending'`, `asyncio.CancelledError` non era gestito e poteva lasciare un lead bloccato senza recap.

### Fix
- `campaign_orchestrator.py`: aggiunto cleanup su `asyncio.CancelledError` solo quando il tentativo e' ancora provabilmente pre-send (`message is None`, `pending`, `retry`). Non tocca i casi `sending`, che restano al recovery conservativo anti-duplicato.
- `campaign_orchestrator.py` + `account_lease.py`: ogni invocazione usa un lease owner univoco; durante `Retry(defer=...)` il lease resta attivo fino a pochi secondi prima della ripartenza per impedire job duplicati immediati, senza bloccare il job differito corretto.
- `work_enqueue.py` + `recovery_checker.py`: il recovery riaccoda solo se Redis non contiene gia' `arq:job`, `arq:retry` o `arq:in-progress` per quello stesso `(campaign, account)`.
- `instagram_page.py`: rilevazione Storie ampliata a URL e input/aria label "Reply/Rispondi"; chiusura con Escape, pulsante Close/Chiudi e fallback a navigazione diretta al profilo. Se la ricerca input DM finisce nel viewer Storie, torna al profilo e ritenta una sola volta.
- Aggiornati `CLAUDE.md`, `INDEX.md`, `PROGRESS.md` e memoria persistente.

### Nota operativa
- I processi ARQ gia' avviati non ricaricano il codice automaticamente. Non riavviare il worker mentre un DM e' in corso: attendere pausa/finestra sicura o mettere in pausa da UI, poi riavviare worker DM e cron worker.

### Verifica
- `python -m py_compile app/services/campaign_orchestrator.py app/services/account_lease.py app/services/work_enqueue.py app/services/recovery_checker.py app/browser/instagram_page.py tests/test_campaign_orchestrator.py tests/test_instagram_page.py tests/test_operator_guardrails.py` OK.
- `python -m pytest tests/test_campaign_orchestrator.py tests/test_instagram_page.py tests/test_recovery_checker.py tests/test_operator_guardrails.py` OK (`12 passed`).
- Lettura live confermata: recap 14:04 -> recovery requeue 14:05 era la causa della ripartenza anticipata.

---

## Come avviare

### Prerequisiti
1. Docker Desktop in esecuzione
2. Ollama installato e in esecuzione (`ollama serve`)
3. Modello scaricato: `ollama pull llama3.2`

### Avvio rapido (Windows)
```
double-click start.bat
```

### Avvio manuale
```bash
# Terminale 1 — Redis
docker-compose up -d

# Terminale 2 — Backend
cd backend
venv\Scripts\activate
uvicorn app.main:app --reload --port 8000

# Terminale 3 — Worker ARQ DM
cd backend
venv\Scripts\activate
arq app.workers.task_queue.WorkerSettings

# Terminale 4 — Worker ARQ cron
cd backend
venv\Scripts\activate
arq app.workers.cron_worker.CronWorkerSettings

# Terminale 5 — Frontend
cd frontend
npm run dev
```

### URL
- Dashboard: http://localhost:3000
- API Docs: http://localhost:8000/docs
- Health: http://localhost:8000/api/health

---

## [2026-05-30] Fase 7E — Import profili da lista ✅ COMPLETATA

Branch: `feature/import-profiles`. Permette a una campagna di partire da una lista di profili IG caricata da file (`.txt`/`.csv`) invece che dallo scraping di una pagina target. Il cliente fornisce i profili già selezionati; il bot recupera le bio via account IG e genera/invia i DM col flusso esistente.

### Modello dati
- `campaigns.source_type` (`'scrape'` default | `'import'`); `target_username` reso **nullable** (migrazione 013).
- Nuova tabella staging `imported_profiles` (`pending → resolved | not_found | private | error`, unique `(campaign_id, username)`).
- Migrazione `013_import_profiles.py` applicata a **Supabase** (012 → 013). Durante l'applicazione individuata e terminata una connessione zombie `idle in transaction` (~10 giorni) che teneva un lock su `campaigns` e mandava in timeout l'`ALTER TABLE`.

### Backend
| File | Contenuto |
|---|---|
| `backend/alembic/versions/013_import_profiles.py` | Migrazione source_type + imported_profiles |
| `backend/app/models/imported_profile.py` | Modello staging `ImportedProfile` |
| `backend/app/utils/ig_username.py` | Parser puro URL/@handle/username/CSV (+ test) |
| `backend/app/services/import_resolver.py` | `store_imported_lines`, `classify_resolution`, loop async `resolve_imports` (riusa login/rotazione-429/session-break/kill-switch dello scraper) |
| `backend/app/workers/import_worker.py` | `resolve_imports_task` (ARQ) |
| `backend/app/services/work_enqueue.py` | `enqueue_resolve` (job id `resolve:{campaign_id}`) |
| `backend/app/api/campaigns.py` | `POST /import-profiles` (upload), `GET /import-status`, branch import in `start-scrape` |

### Frontend
- `lib/types.ts` / `lib/api.ts`: `source_type`, `ImportStatusResponse`, `importProfiles` (fetch multipart), `importStatus`.
- Form nuova campagna: toggle "Scraping pagina | Lista importata" + upload file.
- Dettaglio campagna: pannello contatori import (pending/resolved/not_found/private/error) + label "Risoluzione profili".

### Note
- Dedup `global_contacts`: NON a resolve-time (ig_user_id noto solo dopo la call IG); resta la dedup a send-time del worker DM. Profilo privato → `Follower` creato comunque.
- Test: `test_ig_username.py` (7) + `test_import_resolver.py` (4). Suite completa: 31 passed.
- Realineata la documentazione: il DB di produzione è **Supabase Postgres**, non SQLite (SQLite resta solo fallback dev). Corretti `CLAUDE.md` e questo file.

### Fix post-audit (commit a0ed1ed)
- **resume/reenqueue dispatch** (BLOCCANTE): nuovo `enqueue_collection` instrada import→`resolve_imports_task`, scrape→`scrape_followers_task`. Usato in `resume_campaign_control` e `reenqueue_active_work` (boot/unhalt) — prima lanciavano lo scraper su campagne import (`target_username=None`) → errore.
- **reset import-aware** (BLOCCANTE): import con lead → `ready`; import senza lead → `draft` + import a `pending`. Niente campagna incastrata.
- **no DM parallelo per import** (ALTO): `start_dm_auto` rifiuta import + bottone nascosto → import a fase singola (risolvi → ready → /start).
- **UI**: "Lista importata" invece di `@null` (lista campagne, filtro leads, recap Telegram).
- **Non fatto di proposito**: resolver a batch brevi/defer (#4) e slot account su rotazione (#5) rispecchiano lo scraper esistente; vanno rifattorizzati su scraper+resolver insieme in un task dedicato per non divergere. Endpoint "ritenta errori import" (#6) = feature futura.

---

## Storico audit

| Data | File corrente | Scope | Esito |
|---|---|---|---|
| 2026-04-14 | `ANTI_DETECTION.md` | Sicurezza, privacy, policy IG, proxy, vettori detection, ban | Documento permanente — non è un audit di codice ma una guida operativa sui rischi |
| 2026-04-15 | `AUDIT.md` v1 | Bug analysis completa (10 bug), sicurezza API, compliance GDPR | 10 bug trovati — tutti risolti nella stessa sessione |
| 2026-04-16 | `AUDIT.md` v2 | Audit completo sistema: backend, frontend, DB, config, flussi utente | 33 bug trovati (4 critici, 8 alti, 12 medi, 9 bassi), 5 feature parziali, 7 rischi futuri, 15 miglioramenti. Creato `FUTURE_IMPROVEMENTS.md`. |

**Regola**: `AUDIT.md` contiene sempre e solo l'audit più recente. Quando viene fatto un nuovo audit, il vecchio viene riassunto qui con data + esito, e `AUDIT.md` viene sovrascritto.
