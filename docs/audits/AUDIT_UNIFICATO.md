# AUDIT UNIFICATO — BOT OUTBOUND

**Data**: 2026-05-18
**Fonti**: review Claude (Opus 4.7) + review Codex, fuse e deduplicate.
**Scope**: backend (servizi, worker, browser, API, modelli, migrazioni) + frontend + DB + config.
**Stato git**: nessun repository git nel checkout → branch/diff non auditabili.
**Verifiche ambiente (Codex)**: `compileall app` OK · `npm run lint` OK con warning · `npm run build` OK solo con rete (Google Fonts) · `pytest` non eseguibile (modulo mancante) · DB corrente `backend/data/bot.db`, Alembic a `007`.

Legenda fonte: `[C]` Claude · `[X]` Codex · `[C+X]` entrambe.

---

## Decisioni di approccio (conflitti risolti con l'utente)

| Tema | Decisione | Motivazione |
|---|---|---|
| Reservation/recovery DM | **Fix minimo ora**, refactor `contact_reservations` come intervento strategico | Fermare subito il leak con basso rischio; lo split "lavorato vs prenotato" resta pianificato |
| Modello queue/worker ARQ | **Combinato a fasi**: F1 = worker DM short-lived + coda cron dedicata + stop delete `arq:in-progress`; F2 = lease DB + heartbeat + cancel cooperativo | La starvation cron è il rischio sistemico dominante; short-lived risolve anche sessione DB lunga e orphan-restart |
| Legacy auth (`JWT_SECRET` vuoto) | **Rimuovere del tutto la legacy mode**; auth sempre obbligatoria | Elimina alla radice "API aperta", risolve incoerenza col frontend; dev coperto da `scripts/create_admin.py` |

---

## 1. Executive summary

Sistema di outbound Instagram: scraping follower/following + bio, generazione messaggi AI multi-provider, invio DM via browser persistente Patchright, tracking lead/risposte, control-plane web/Telegram. Architettura async sensata (FastAPI + ARQ/Redis + SQLAlchemy 2 + instagrapi + Next.js, DB SQLite/Postgres-Supabase) e con molte iterazioni di hardening. Backend compila, frontend builda. **Restano difetti sistemici gravi**, concentrati nelle transizioni di stato critiche (invio DM, recovery, reservation globale, lifecycle job) e nella sicurezza/operatività: segreti di produzione in chiaro; auth disattivabile; starvation dei cron ARQ; cursore scraping non persistito; approvazione bypassata in auto-gen; reply-checker cieco e con falsi positivi; config UI ignorate dal backend; bug fingerprint che rompe la pagina. Zero test automatici.

## 2. Mappa architettura

Frontend Next.js → API FastAPI (persistenza campaign/account/follower/message) → worker ARQ (scraping + DM) → instagrapi (scrape/bio/inbox) → AI provider (messaggi) → Patchright (invio DM) → tracking `messages`/`followers`/`global_contacts` → reply/recovery checker → anomalie + kill-switch.

Componenti chiave: `app/api/*`, `app/models/*`, [campaign_orchestrator.py](backend/app/services/campaign_orchestrator.py), [scraper.py](backend/app/services/scraper.py), [task_queue.py](backend/app/workers/task_queue.py), [work_enqueue.py](backend/app/services/work_enqueue.py), [context_manager.py](backend/app/browser/context_manager.py), [instagram_page.py](backend/app/browser/instagram_page.py), [api.ts](frontend/lib/api.ts).

---

## 3. Problemi per priorità

### 🔴 CRITICO

**C1 — Segreti di produzione live, in chiaro, duplicati** `[C]`
- Dove: [.env](../../.env), [backups/BOT_OUTBOUND_BACKUP/.env](../../backups/BOT_OUTBOUND_BACKUP/.env), [.vercel/project.json](../../.vercel/project.json).
- Problema: password Supabase nel `DATABASE_URL`, chiave Fernet `SECRET_KEY` (cifra le password IG), `JWT_SECRET`, token bot Telegram, API key Groq — in chiaro e duplicati nel backup. `SECRET_KEY` + DB = tutte le password account IG decifrabili.
- Impatto: se la cartella finisce in un repo (`gh`/`ultrareview` bundlano il branch) o il backup viene condiviso → compromissione totale. Valori già esposti in output tool in sessione.
- Fix: (1) **ruotare subito** tutti i segreti (Supabase, Fernet — richiede re-encrypt password account, JWT, Telegram, Groq); (2) `.gitignore` root con `.env`, `backups/BOT_OUTBOUND_BACKUP/`, `.vercel/`, `backend/data/`; (3) eliminare la cartella backup dal working dir; (4) secret manager / env del provider.

**C2 — Auth disattivabile a runtime → API completamente aperta** `[C+X]`
- Dove: [auth_deps.py:26-44](backend/app/utils/auth_deps.py#L26-L44), [main.py:142-158](backend/app/main.py#L142-L158), [vercel.json](vercel.json) (backend su `/_/backend`); incoerenza frontend [AuthGuard.tsx:16](frontend/components/AuthGuard.tsx#L16) (obbliga login mentre il backend permette legacy admin).
- Problema: con `JWT_SECRET` vuoto ogni endpoint è pubblico (campagne/account/lead PII/halt). Singolo punto di fallimento in deploy.
- **Decisione**: rimuovere la legacy mode → `get_current_user` richiede sempre token valido; rimuovere `_LEGACY_USER`; il frontend resta coerente. Bootstrap admin via `scripts/create_admin.py`.

**C3 — Starvation dei cron ARQ: recovery e sicurezza non girano sotto carico** `[C]` (+ correlato a `[X]` n.3)
- Dove: [task_queue.py:266-278](backend/app/workers/task_queue.py#L266-L278) — `max_jobs=10`, `job_timeout=8h`; `run_campaign_task` long-lived.
- Problema: i cron (`release_stale_locks`, `recover_sending`, `check_replies`, `daily_reset`, `telegram_commands`) condividono il pool dei worker DM. ~10 account → tutti gli slot occupati da job 8h → cron accodati per ore: lock mai rilasciati, `sending` mai riconciliati, kill-switch Telegram muto.
- **Decisione (F1)**: worker DM short-lived (job = 1 batch breve, ri-accodato con delay) + coda/processo ARQ dedicato ai cron; **F2**: lease DB + heartbeat.

**C4 — Riavvio API uccide tutte le campagne attive** `[C]`
- Dove: [main.py:43-76](backend/app/main.py#L43-L76) `_auto_pause_orphaned_campaigns` nel lifespan.
- Problema: il lifespan API mette in `paused` le campagne attive assumendo "nessun worker", ma ARQ è processo separato; ogni restart/redeploy uvicorn ferma worker vivi.
- Fix: rimuovere l'auto-pause dal lifespan; lasciare la crash-recovery al solo cron `release_stale_locks` (criterio inattività 30 min). Naturalmente coerente col modello short-lived (C3).

**C5 — Recovery DM non recuperabile + reservation leak** `[X]`
- Dove: [campaign_orchestrator.py:~400](backend/app/services/campaign_orchestrator.py#L400) (`message.status='sending'` committato **prima** di `account_id`), [recovery_checker.py:91](backend/app/services/recovery_checker.py#L91), [campaign_orchestrator.py:1085-1093](backend/app/services/campaign_orchestrator.py#L1085-L1093).
- Problema: se il processo cade tra il commit `sending` e il salvataggio `account_id`, il recovery trova `account_id=null` e non può verificare l'invio; inoltre quando rimette in retry **non rilascia** la reservation in `global_contacts`. Nel DB già 11 `global_contacts` senza `last_contacted_at` e history vuota — coerente col failure mode.
- Impatto: lead persi/bloccati silenziosamente.
- **Decisione (fix minimo ora)**: salvare `account_id` nella **stessa** transizione che porta a `sending`; nel recovery, in assenza di evidenza di delivery, rilasciare esplicitamente la reservation (`_release_global_contact_reservation`) prima di `retry`. **Strategico**: tabella `contact_reservations` (owner job, expiry, stato) separata da "contatto lavorato" + state machine DM esplicita.

**C6 — Fingerprint browser: getter ricorsivo rompe `measureText()`** `[X]`
- Dove: [context_manager.py:348-357](backend/app/browser/context_manager.py#L348-L357).
- Problema: `Object.defineProperty(metrics,'width',{get:()=>metrics.width+noise})` — il getter rilegge `metrics.width` = se stesso → ricorsione infinita → `RangeError`, JS di pagina rotto, fingerprint **più** rilevabile.
- Fix: `const originalWidth = metrics.width;` **prima** di `defineProperty`, poi `get: () => originalWidth + noise`.

### 🟠 URGENTE

**U1 — Approvazione messaggi bypassata in auto-gen/parallelo** `[C]`
- Dove: [campaign_orchestrator.py:991-1042](backend/app/services/campaign_orchestrator.py#L991-L1042) `_get_or_create_message` (non controlla `require_approval`); confronto con [followers.py:144](backend/app/api/followers.py#L144) e [task_queue.py:46](backend/app/workers/task_queue.py#L46). `_apply_approval_sampling` mai chiamato (dead code).
- Problema: in `auto_generate=True` (`scraping_and_running`, `start-dm-auto`) i DM partono senza passare per `pending_approval`.
- Fix: in `_get_or_create_message`, se `campaign.require_approval` e messaggio nuovo → `pending_approval` e non inviabile; oppure vietare lato API la combinazione auto-gen + approval.

**U2 — Cursore scraping non persistito + scraping parziale marcato completo** `[C+X]`
- Dove: [scraper.py:246-388](backend/app/services/scraper.py#L246-L388) (`max_id` locale), [scraper.py:358-388](backend/app/services/scraper.py#L358-L388) (break su 429 → flusso superiore marca `ready`/`completed`).
- Problema: dopo crash/redeploy/ARQ-retry lo scraping riparte da pagina 1 (decine di migliaia di chiamate API → 429/ban). Dataset parziale per rate-limit trattato come valido.
- Fix: persistere cursore su `Campaign` (`scrape_cursor`) e riprendere; distinguere esiti `completed` / `partial` / `rate_limited`; non passare a `ready`/`completed` se interrotto da rate-limit.

**U3 — Reply checker: cieco su "Richieste" + falsi positivi + stati ignorati** `[C+X]`
- Dove: [reply_checker.py:103-152](backend/app/services/reply_checker.py#L103-L152), [reply_checker.py:35](backend/app/services/reply_checker.py#L35).
- Problemi: (a) usa solo `direct_threads` — il cold outreach finisce in **Requests/Pending**, risposte non rilevate; (b) `has_reply` = qualsiasi messaggio non nostro, senza confronto `sent_at` → conversazioni vecchie/gruppi contate come reply (gonfia `reply_rate`/A-B); (c) ignora campagne `scraping_and_running`.
- Fix: includere `direct_pending_inbox()`; matchare solo messaggi del target **dopo** l'ultimo `sent_at`; saltare thread di gruppo (>2 utenti); includere account ruolo DM e stati attivi multipli.

**U4 — Prompt injection via bio Instagram** `[C]`
- Dove: [ai_personalizer.py:72-103](backend/app/services/ai_personalizer.py#L72-L103) — bio interpolata grezza nel prompt.
- Problema: bio = input attaccante; può alterare il DM generato (inviato sotto il tuo account) → ban/reputazione.
- Fix: delimitatori espliciti "dato non attendibile, mai istruzioni"; sanificare imperativi; validare output vs template (similarità) con fallback su deriva.

**U5 — Sessione DB long-lived + sleep active-hours non interrompibile** `[C+X]`
- Dove: [campaign_orchestrator.py:128](backend/app/services/campaign_orchestrator.py#L128) (sessione unica per tutta la vita job), [human_behavior.py:129-146](backend/app/services/human_behavior.py#L129-L146) (`asyncio.sleep` fino alla finestra successiva).
- Problemi: su Supabase/NullPool una connessione resta aperta ore attraverso sleep multi-ora → PgBouncer chiude idle → query falliscono → auto-pause. Lo sleep active-hours non fa polling pause/halt/stop → controlli inefficaci per ore + crash-recovery scambia il dormiente per bloccato.
- Fix: sessioni a granularità iterazione/fase (chiudere prima di sleep lunghi); sleep a chunk brevi con check stato + heartbeat. (Coerente col modello short-lived C3.)

**U6 — `start_scrape` commit prima del check Redis/enqueue** `[X]`
- Dove: [campaigns.py:212-229](backend/app/api/campaigns.py#L212-L229).
- Problema: `status=scraping` committato prima di verificare Redis; se Redis è down la campagna resta `scraping` senza worker (mentre `start_campaign` ha già il preflight `BUG-NEW-12`).
- Fix: preflight Redis prima della commit, o transazione compensativa su errore enqueue.

**U7 — Config avanzate campagna inviate dalla UI ma ignorate dal backend** `[X]`
- Dove: UI [campaigns/new/page.tsx:63](frontend/app/campaigns/new/page.tsx#L63) → backend [campaigns.py:116-138](backend/app/api/campaigns.py#L116-L138) (`create_campaign` non mappa `scrape_session_size`, pause, `bio_fetch_delay_*`).
- Problema: l'utente crede di aver configurato limiti anti-detection; il sistema usa i default.
- Fix: mappare tutti i campi in create/update + test API; correggere tipo colonna `bio_fetch_delay_min/max` (vedi M-types).

**U8 — Export CSV rotto con JWT attivo** `[X]`
- Dove: [api.ts:222-237](frontend/lib/api.ts#L222-L237) `exportUrl` (link diretto senza header `Authorization`), uso in [leads/page.tsx:65](frontend/app/leads/page.tsx#L65).
- Fix: `fetch` autenticato → Blob download, oppure URL firmato temporaneo lato backend.

**U9 — `_fallback_message` può inviare placeholder non sostituiti** `[C]`
- Dove: [ai_personalizer.py:223-308](backend/app/services/ai_personalizer.py#L223-L308).
- Problema: il check `if "{" in message and "}" in message` ritorna il fallback **non ri-validato**, che sostituisce solo `{name}{nome}[Nome][nome]`; `[Nome]` non è nemmeno coperto dal check `{}` → DM con segnaposto letterale.
- Fix: fallback che sostituisce tutti i pattern noti + check finale: se restano `{...}`/`[...]` → follower `failed`, non inviare.

**U10 — Dipendenze runtime incoerenti / ambiente non riproducibile** `[C+X]`
- Dove: [requirements.txt:34-36](backend/requirements.txt#L34-L36) (`patchright`/`humanization-playwright` commentati ma core), [pyproject.toml:10](backend/pyproject.toml#L10) (mancano dipendenze runtime/test), `pytest` non installabile.
- Fix: una sola fonte di verità dipendenze + lockfile + CI install da zero + `pytest` nelle dev-deps.

### 🟡 MEDIO

**M1 — Pause scraping: bug del modulo** `[C]` — [scraper.py:316](backend/app/services/scraper.py#L316) `total % session_size == 0`: `total` cresce di soli nuovi → può scavalcare il multiplo (pausa mai presa, sessione illimitata) o, in re-scrape (`batch_total=0`), restare sul multiplo → pause ripetute a ogni iterazione. Fix: contatore "profili dall'ultima pausa".

**M2 — Soft-block counter non realmente consecutivo** `[X]` — [scraper.py:436](backend/app/services/scraper.py#L436): `consecutive_soft_blocks` non resettato dopo un `user_info` riuscito → 3 blocchi non consecutivi fermano il batch. Fix: reset su bio fetch ok.

**M3 — Indici DB insufficienti** `[C+X]` — mancano: `messages(account_id,status,sent_at)`, `messages(follower_id,status)`, recovery `messages(status,updated_at)`, `campaign_accounts(account_id)`, `activity_logs(created_at)`. Query hot a ogni iterazione worker → degrado con migliaia di righe. Fix: migrazione dedicata + `EXPLAIN` sulle query operative.

**M4 — `ORDER BY func.random()` per ogni claim** `[C]` — [campaign_orchestrator.py:786](backend/app/services/campaign_orchestrator.py#L786): O(n) ripetuto per ogni DM su set grandi. Fix: campionamento su finestra / colonna `rand_order` indicizzata.

**M5 — Tipi colonna errati** `[C+X]` — [campaign.py:51-53](backend/app/models/campaign.py#L51-L53): `bio_fetch_delay_*` `Mapped[float]` su `Integer` (troncamento su Postgres); `require_approval`/`auto_generate` `bool` su `Integer`. Fix: `Float`/`Boolean` + migrazione.

**M6 — AI failure transitorio brucia il follower a `failed`** `[C]` — [campaign_orchestrator.py:1037-1041](backend/app/services/campaign_orchestrator.py#L1037-L1041): 429 free-tier esaurisce i retry → follower `failed` definitivo. Fix: errori transitori → restare `bio_scraped` per retry.

**M7 — `reset_campaign` perde stato e storico** `[C]` — [campaigns.py:314-359](backend/app/api/campaigns.py#L314-L359): riporta anche `sent`/`replied` a `bio_scraped` e cancella tutti i `Message` (incoerente col commento "kept"). Fix: resettare solo non-`sent`/`replied`, conservare storico.

**M8 — Migrazioni Alembic a ogni boot API** `[C]` — [main.py:79-93](backend/app/main.py#L79-L93): race con istanze multiple/serverless. Fix: migrazioni come step di deploy separato.

**M9 — Daily reset ignora `scraping_and_running`** `[X]` — [task_queue.py:133-160](backend/app/workers/task_queue.py#L133-L160): solo `status==running` riavviato → campagne parallele non ripartono dopo reset. Fix: includere stati attivi multipli.

**M10 — Health check legato a Ollama con provider cloud** `[X]` — [health.py:18](backend/app/api/health.py#L18): deployment Groq/Gemini risulta degradato. Fix: health in base ad `AI_PROVIDER`.

**M11 — Dead code / docstring fuorvianti** `[C]` — `get_next_account` ([account_manager.py:114](backend/app/services/account_manager.py#L114)) e `_apply_approval_sampling` ([ai_personalizer.py:459](backend/app/services/ai_personalizer.py#L459)) senza chiamanti; `record_failure` promette cooldown ma solo logga ([account_manager.py:178-194](backend/app/services/account_manager.py#L178-L194)).

**M12 — Token JWT in localStorage** `[C]` — [api.ts:17-34](frontend/lib/api.ts#L17-L34): esfiltrabile via XSS, nessuna revoca, scadenza 7gg. Fix: valutare cookie httpOnly + CSRF o scadenza breve + refresh.

**M13 — Rate-limit login inefficace dietro proxy** `[C]` — [auth.py:27-46](backend/app/api/auth.py#L27-L46): dietro proxy un solo bucket (DoS) o bypass via `X-Forwarded-For`; in-memory per processo. Fix: limite per email+IP, trust forwarded-for solo proxy whitelist, contatore su Redis.

### 🟢 BASSO

- **B1** `[C]` `_fetch_followers_chunk` fallback carica tutto in una call sincrona su target enormi — [scraper.py:391-413](backend/app/services/scraper.py#L391-L413).
- **B2** `[C+X]` Nessun test automatico (`backend/tests/` vuoto) nonostante Fase 6.
- **B3** `[C+X]` CORS `allow_methods/headers=["*"]`, `allow_credentials=True` ampio (auth via Bearer header).
- **B4** `[C]` `delete_campaign` consentito in `scraping`/`paused` con job in volo → cascade sotto i piedi; bloccare anche `scraping`/`scraping_and_running` (`[X]` n.3).
- **B5** `[C]` `_validate_message` collassa newline → template multi-riga perdono struttura (valutare invio multi-parte Shift+Enter).
- **B6** `[C+X]` Versioni dipendenze non pinnate (`>=`) → build non riproducibili.
- **B7** `[X]` Warning lint frontend, import/funzioni inutilizzati, `<img>` da sostituire.
- **B8** `[X]` Build frontend dipende da Google Fonts in rete → fallisce offline. Fix: font self-hosted.
- **B9** `[X]` Checkout non-git + generated dirs non protette. Fix: repo pulito + `.gitignore` root + CI.

---

## 4. Bug nascosti / edge case

- `sending` senza `account_id` + reservation mai rilasciata (C5) — failure mode già osservato nel DB (11 placeholder).
- Job ARQ duplicabili: delete di `arq:in-progress` non ferma il coroutine ma lo nasconde → scraping doppio / sessione browser-account contesa ([work_enqueue.py:32-68](backend/app/services/work_enqueue.py#L32-L68), [campaigns.py:186-206](backend/app/api/campaigns.py#L186-L206)).
- `scraping_and_running` non transita mai a `completed` (`_maybe_complete_campaign` filtra `status==running`, [campaign_orchestrator.py:964-975](backend/app/services/campaign_orchestrator.py#L964-L975)).
- Account forzato su più campagne → sessione browser condivisa; timezone fingerprint casuale non coerente con proxy/account; login automatico via credenziali può innescare challenge.
- `daily_reset` 00:05 UTC vs conteggi UTC-midnight con `timezone_offset_hours=2` → limite "salta" a notte fonda locale.
- Stagger 0–15min: pausa durante lo stagger non rilevata fino a fine sleep (pre-check è prima dello stagger).
- `_human_type` typo+Backspace con popup menzione/emoji aperto → testo corrotto inviato.
- `_pre_send_check` riusa la sessione worker mid-transaction senza `expire_all` → possibile lettura stale in contesa.

## 5. Miglioramenti architetturali

1. Separare "contatto lavorato" da "reservation temporanea": tabella `contact_reservations` (owner job, expiry, stato, cleanup) — **strategico** (post fix minimo C5).
2. State machine DM esplicita e testata: `pending → reserved → sending(account_id obbligatorio) → sent | retry | failed | unknown`, transizioni atomiche.
3. Queue idempotente: niente delete `in-progress`, lease DB + heartbeat + cancel cooperativo (F2 della decisione queue).
4. Worker short-lived + coda cron dedicata (F1) — risolve C3/C4/U5.
5. Adapter isolati dietro interfacce per Instagram / browser / AI → testabilità di orchestrazione e recovery senza servizi reali.
6. Repository/service layer per accessi DB (claim, daily_sent, dedup) → testabilità + ottimizzazione query centralizzata.
7. Page Object Instagram resiliente: selettori centralizzati + healthcheck "layout cambiato" → anomalia invece di bruciare follower.
8. Config tipizzata/validata (Boolean/Float, `min<=max`, `active_hours_start<end`).
9. Osservabilità operativa: dashboard stuck jobs, placeholder contacts, `sending` vecchi, account/proxy health, rate-limit, anomalie.

## 6. Funzionalità aggiuntive

Inbox risposte unificata (Requests + threading) → CRM leggero; warm-up comportamentale (azioni non-DM); proxy health & rotazione con disabilitazione auto su proxy down; A/B test con significatività + auto-winner; spintax/varianti template (anti content-detection); scheduling per fasce orarie; lead scoring pre-invio; suppression list/blacklist import; review queue ricca; report deliverability per campagna; webhook/Telegram su reply lead; dry-run/shadow mode.

## 7. Roadmap di implementazione

### Fase 0 — Immediati (sicurezza/operatività, prima di ogni altra cosa)
1. **C1** rotazione segreti + `.gitignore` + rimozione backup/dati dal working dir.
2. **C2** rimuovere legacy auth (auth sempre obbligatoria) + allineare frontend.
3. **C5** fix minimo: `account_id` nella transizione a `sending` + rilascio reservation su retry/fail nel recovery.
4. **C6** fix getter `measureText`.
5. **C4** rimuovere auto-pause dal lifespan API.
6. **U6** preflight Redis in `start_scrape`.
7. **U7** mappare config avanzate in create/update (+ M5 tipi colonna).
8. **U8** export CSV autenticato.
9. **U10** dipendenze corrette + `pytest` installabile + build frontend offline (B8).

### Fase 1 — Breve termine (robustezza)
10. **C3/C4/U5** worker DM short-lived + coda/processo cron dedicato + stop delete `arq:in-progress` + sleep interrompibili.
11. **U1** stop invio non approvato in auto-gen.
12. **U2** cursore scraping persistito + stati `partial`/`rate_limited`.
13. **U3** reply-checker su Requests + match temporale + stati/ruoli.
14. **U4** mitigazione prompt injection.
15. **U9** fallback placeholder safe.
16. **M1/M2/M3/M4/M9/M10** modulo pause, soft-block reset, indici, ordering claim, daily_reset stati, health per provider.
17. Suite test minima su state machine DM, claim atomico, dedup, `_validate_message`, `apply_safety_caps`, pause/resume, recovery.

### Fase 2 — Strategico
18. Tabella `contact_reservations` + state machine DM completa.
19. Account lease manager + heartbeat + cancel cooperativo.
20. Adapter modulari Instagram/AI/browser + repository layer.
21. Osservabilità operativa + schema Postgres pulito con migrazioni affidabili + CI.
22. Funzionalità di prodotto (inbox unificata, warm-up comportamentale, proxy health, lead scoring, suppression list).

## 8. Le azioni più importanti da fare subito

1. **C1** mettere in sicurezza e ruotare i segreti (esposti e duplicati).
2. **C2** rimuovere la legacy auth (chiude "API aperta" + incoerenza frontend).
3. **C5** transizione `sending` atomica con `account_id` + rilascio reservation su retry/fail (stop lead bruciati).
4. **C3/C4** sbloccare i cron (short-lived + coda dedicata) e togliere l'auto-pause al restart API.
5. **C6 + U7** fix getter `measureText` e allineamento UI/API/schema config avanzate.

---

*Aree che richiedono verifica manuale*: rami eccezione di `send_dm` (idempotenza global_contacts), comportamento Supabase su sleep lunghi, selettori Instagram correnti, conteggio reale `global_contacts` placeholder nel DB.
