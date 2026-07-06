# INDEX — BOT OUTBOUND

**Punto di partenza per qualsiasi agente AI** che lavora su questo progetto.  
Leggi questo file prima di qualsiasi altro.

---

## Ordine di lettura consigliato

```
1. INDEX.md          ← sei qui — panoramica e stato globale
2. CLAUDE.md         ← architettura completa, stack, schema DB, principi anti-detection
3. docs/project/PROGRESS.md       ← storia cronologica: cosa è stato fatto, quando, e perché
4. docs/audits/AUDIT.md           ← ultimo audit codice (sempre aggiornato — storico in PROGRESS.md)
5. docs/architecture/FUTURE_IMPROVEMENTS.md ← miglioramenti proposti, pronti per sviluppo
6. docs/architecture/AI_MESSAGES.md         ← tutti i parametri per migliorare la qualità dei messaggi AI
7. docs/architecture/ANTI_DETECTION.md      ← guida permanente: vettori detection IG, proxy, ban, sicurezza operativa
8. docs/guides/GUIDA.md           ← guida operativa per l'utente finale (non rilevante per sviluppo)
```

**Regola**: se devi fare una modifica al codice, leggi sempre CLAUDE.md (architettura) + la sezione più recente di `docs/project/PROGRESS.md` prima di toccare qualsiasi file.
Rileggi anche il codice del flusso coinvolto: se il contesto e' indietro rispetto al codice verificato, preserva i guardrail esistenti e aggiorna i documenti rilevanti nello stesso task.

---

## Stato globale del progetto

### Fasi completate ✅

| Fase | Descrizione | Data |
|---|---|---|
| 1 | Foundation: FastAPI, DB, models, schemas, API accounts | 2026-04-14 |
| 2 | Scraping follower con instagrapi | 2026-04-14 |
| 3 | AI personalizzazione messaggi (Ollama) | 2026-04-14 |
| 4 | Engine invio DM (Patchright + anti-detection) | 2026-04-14 |
| 5 | Frontend Next.js dashboard completa | 2026-04-14 |
| Fix sessione 2 | Bug critici + ottimizzazioni anti-detection + lead database | 2026-04-15 |
| 7A | Multi-account per campagna (optimistic lock SQLite WAL) | 2026-04-15 |
| Audit v1 | Security, compliance, bug analysis (10 bug — tutti risolti) | 2026-04-15 |
| Audit v2 | Audit completo sistema (33 bug, 15 miglioramenti) | 2026-04-16 |
| Fix operativi | Message button fix, manual login, reply checker, A/B test, approval queue, pre-gen, leads page, requeue, live log | 2026-04-16 |
| 7C | Pagina `/leads` con export CSV + filtri | 2026-04-16 |
| Migliorie | Browser overlap fix, auto-pause al boot, AI quote fix, pre-gen events, UI performance, DM count fix | 2026-04-16 |
| Control-plane remoto | Kill-switch web completo + Telegram pause/resume per campagna con bottoni inline | 2026-05-11 |
| Supabase auth fix | Pooler-safe asyncpg + admin dashboard ricreato su Supabase | 2026-05-11 |
| 7E | Import profili da lista (source_type=import, imported_profiles, resolve worker) | 2026-05-30 |
| 7F | Scraping avanzato: contatti (phone/email/whatsapp/link) + messaggistica opzionale + cap scraping | 2026-06-06 |
| 7G | Qualifica lead: target salvabili, scoring deterministico, AI sugli ambigui, export dedicato; migration 015 applicata | 2026-06-08 |
| 7G fix | Lead qualification hardening: paginazione worker ID-based, cancellazione run, fix _model_used, JSON parse guard | 2026-06-08 |
| Hardening DM | Resilienza blip rete/DB (Retry defer), a-capo DM via Shift+Enter, switch AI Groq→Gemini 2.5-flash, fix freeze input-DM, typing 2.3x | 2026-06-22 |
| 7H | Motore Fase Bio via browser (`bio_engine='browser'`, Patchright + `web_profile_info`, no consumo cap API); migration 022 applicata | 2026-07-06 |

### Da fare ☐

| Priorità | Task | Note |
|---|---|---|
| **MEDIA** | Fase 7B: multi-campagna parallela | 1 account = 1 campagna alla volta |
| **BASSA** | Test unitari | timing, account_manager, ai_personalizer |
| **BASSA** | Migrazione Alembic | attualmente create_all + inline ALTER |
| **BASSA** | Logging strutturato | livelli loguru |

---

## Architettura chiave (sintesi)

### Multi-account per campagna (Fase 7A)
- `campaign_accounts` junction table con `daily_limit_override`, `is_active`
- 1 ARQ task per account assegnato, deduplicato con `_job_id`
- Optimistic lock atomico su `followers.locked_by_account_id`
- Crash recovery: stale lock timeout 20 min + cron 15 min
- Startup guard nel worker DM: pausa lavoro attivo stale ereditato da processi precedenti finche' l'operatore lo riprende.
- Worker DM short-lived: processano un batch e usano ARQ `Retry(defer=...)` tra le sessioni.
- Durante la pausa sessione il lease account resta attivo fino a poco prima del defer ARQ, cosi' job duplicati immediati non aggirano il recap/ripartenza prevista.
- Recovery `messages.status='sending'` ogni 5 min nel cron worker dedicato; quando risolve una riga riaccoda il worker solo se campagna, account e assegnazione DM sono ancora validi e Redis non ha gia' job/retry/in-progress per quello stesso account.

### Browser safety
- Mutex asyncio per-account in `context_manager.py` — 1 browser alla volta per account
- ARQ `_job_id` dedup — impedisce worker duplicati per stesso (campaign, account)

### Osservabilità
- `emit()` via Redis per eventi in tempo reale (worker log, pre-gen progress)
- Reply checker cron ogni 30 min con logging diagnostico
- Worker emette eventi anche per errori inattesi

### Control-plane remoto
- Sidebar web admin: **Blocca tutto** attiva il kill-switch globale; **Sblocca** lo disattiva.
- Telegram: `/pause` e `/resume` mostrano bottoni inline per scegliere la singola campagna; `/halt` e `/unhalt` sono i soli comandi globali.
- Problemi su un singolo account (`cooldown`, `challenge_required`, `banned`) isolano l'account e pausano solo le campagne senza altri account DM utilizzabili.

---

## File sorgente critici (backend)

| File | Ruolo |
|---|---|
| `backend/app/main.py` | FastAPI app, CORS, lifespan |
| `backend/app/services/campaign_orchestrator.py` | Loop principale campagna, state machine, multi-worker |
| `backend/app/services/scraper.py` | Scraping follower + session restore (login sicuro) |
| `backend/app/services/scraping_pool.py` | Pool round-robin account scraping multi-account (Approccio C): pre-login, next(), build/release/save_sessions |
| `backend/app/services/browser_bio.py` | Motore Fase Bio via browser (`bio_engine='browser'`): fan-out per-account, mini-sessioni Patchright, cattura `web_profile_info`, claim atomico pool disgiunti, terminazione campagna |
| `backend/app/services/ai_personalizer.py` | Generazione messaggi via Ollama + batch + approval sampling |
| `backend/app/services/reply_checker.py` | Cron: scansione inbox DM per risposte |
| `backend/app/services/bot_state_service.py` | Kill-switch globale halt/resume |
| `backend/app/services/campaign_control.py` | Pausa/ripresa campagna condivisa da API web e Telegram |
| `backend/app/services/lead_qualification.py` | Qualifica lead: scoring deterministico, AI compiler/classifier, query candidati |
| `backend/app/services/work_enqueue.py` | Re-enqueue ARQ condiviso per resume globale |
| `backend/app/services/recovery_checker.py` | Recovery DM rimasti in `sending` + ripartenza worker DM valido |
| `backend/app/services/telegram_commands.py` | Comandi Telegram con bottoni inline per campagne + halt/unhalt globale |
| `backend/app/services/manual_login.py` | Login browser manuale (Patchright) |
| `backend/app/browser/instagram_page.py` | Page Object Model Instagram (selettori header-scoped) |
| `backend/app/browser/context_manager.py` | Browser pool + mutex per-account + fingerprinting |
| `backend/app/utils/events.py` | Sistema eventi Redis per live log frontend |
| `backend/app/workers/task_queue.py` | ARQ config worker DM, funzioni task e pre-gen |
| `backend/app/workers/lead_qualification_worker.py` | ARQ task per run batch di qualifica lead |
| `backend/app/workers/cron_worker.py` | Cron ARQ dedicato: reset, stale locks, reply check, recovery `sending`, Telegram |
| `backend/app/api/campaigns.py` | CRUD + start/pause/resume/stop + pre-gen + approval queue + A/B stats |
| `backend/app/api/admin.py` | Stato/halt/resume globale admin |
| `backend/app/api/accounts.py` | CRUD + login/manual-login + metrics + dm-count + force-cancel-cooldown |
| `backend/app/api/followers.py` | Lista paginata + skip + regenerate + requeue |
| `backend/app/api/leads.py` | Lead database + export CSV |
| `backend/app/api/lead_qualification.py` | Target profile, stima run, risultati/export qualifica lead |
| `backend/app/config.py` | Tutti i parametri configurabili via `.env` |
| `backend/app/database.py` | Engine DB SQLite/Postgres; Postgres pooler-safe per Supabase |

---

## Descrizione file documentazione

| File | Scopo | Aggiornato |
|---|---|---|
| `INDEX.md` | Navigazione, stato globale, ordine lettura | 2026-06-08 |
| `CLAUDE.md` | Architettura completa, stack, schema DB, anti-detection | 2026-06-08 |
| `docs/project/PROGRESS.md` | Log cronologico implementazioni + riferimento audit | 2026-06-08 |
| `docs/audits/AUDIT.md` | Ultimo audit codice — **sempre sovrascritto** | 2026-04-16 |
| `docs/audits/AUDIT_UNIFICATO.md` | Audit unificato Fase 0/1/2 | 2026-05-18 |
| `docs/architecture/FUTURE_IMPROVEMENTS.md` | Miglioramenti proposti (da audit v2) | 2026-04-16 |
| `docs/architecture/AI_MESSAGES.md` | Parametri qualità messaggi AI | 2026-04-15 |
| `docs/guides/GUIDA.md` | Guida utente operativa | 2026-05-11 |
| `docs/architecture/ANTI_DETECTION.md` | Guida detection IG, proxy, ban | 2026-04-14 |
| `docs/setup/TELEGRAM_SETUP.md` | Setup BotFather e comandi Telegram remoti | 2026-05-11 |
| `docs/setup/PROXY_MOBILE_SETUP.md` | Setup IP residenziale + mobile per profilo (USB tethering, iProxy, VPS tunnel) | 2026-06-06 |

---

## Configurazione `.env` attuale

> ⚠️ Configurazione test aggressivo — ripristinare per produzione

```env
MIN_DELAY_SECONDS=10          # prod: 120
MAX_DELAY_SECONDS=45          # prod: 480
SESSION_MIN_MESSAGES=5        # prod: 10
SESSION_MAX_MESSAGES=12       # prod: 20
SESSION_BREAK_MIN_MINUTES=10  # prod: 30
SESSION_BREAK_MAX_MINUTES=25  # prod: 60
HEADLESS=false                # prod: true
OLLAMA_MODEL=llama3.2         # consigliato: llama3.1:8b (vedi docs/architecture/AI_MESSAGES.md)
```

---

## Avvio rapido

```bash
# 1. Redis
docker-compose up -d

# 2. Backend
cd backend && venv\Scripts\activate
uvicorn app.main:app --reload --port 8000

# 3. Worker ARQ DM
cd backend && venv\Scripts\activate
arq app.workers.task_queue.WorkerSettings

# 4. Worker ARQ cron
cd backend && venv\Scripts\activate
arq app.workers.cron_worker.CronWorkerSettings

# 5. Frontend
cd frontend && npm run dev
```

URLs: Dashboard → http://localhost:3000 | API Docs → http://localhost:8000/docs
