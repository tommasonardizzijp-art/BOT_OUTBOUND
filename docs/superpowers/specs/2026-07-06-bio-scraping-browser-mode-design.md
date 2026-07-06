# Design — Modalità scraping bio via Browser (`bio_engine = browser`)

Data: 2026-07-06
Stato: approvato (brainstorming), pronto per il piano di implementazione
Scope: Fase Bio — estrazione bio/contatti dai `Follower(status=pending)` via browser Patchright, come **motore alternativo e selezionabile** rispetto all'API instagrapi. La Fase Lista, la generazione AI e l'invio DM sono fuori scope.

---

## 1. Obiettivo

Aggiungere alla **Fase Bio** un secondo motore di estrazione, selezionabile per campagna:

- `api` (attuale, default): `fetch_and_store_bio` via instagrapi (`user_info`). Veloce (~1-3s/profilo), ma è il vettore che causa il checkpoint (firma da automazione su endpoint mobile privato — vedi memory `botoutbound-checkpoint-pattern-api`).
- `browser` (nuovo): apre ogni profilo in un browser Patchright reale e cattura i dati dal JSON che Instagram serve al suo stesso frontend. Più lento (~10-18s/profilo mediana), molto più tollerato, non consuma il cap API.

Obiettivo immediato: poterlo **attivare per campagna e testarlo sul campo** senza toccare il path API funzionante. Decisione se/come alternare i due motori (o unificarli) rinviata a dopo il test.

## 2. Decisioni prese in fase di brainstorming

| Tema | Decisione |
|---|---|
| Selezione motore | Campo `Campaign.bio_engine` = `api` \| `browser`, default `api`. Dropdown nella form campagna. Migration additiva. |
| Approccio d'innesto | **A — path browser dedicato**. `scrape_bios()` biforca in cima; se `browser` fa fan-out di N task ARQ (non tocca il path API). Zero rischio regressione. No refactor engine-agnostic per ora. |
| Parallelismo | **1 task ARQ per account** (come i DM), non `gather` in un task — obbligato da `job_timeout=3600s` (vedi nota revisione §6). Partenze **differite** via `_defer_by` (stagger crescente). Ogni account macina un **pool disgiunto** di pending. Pausa lunga = `Retry(defer)`. |
| Assegnazione lavoro | **Claim atomico** via `Follower.locked_by_account_id` (stesso optimistic-lock dei DM). Nessun doppione tra sessioni parallele. |
| Estrazione dati | Riuso di `fetch_and_store_bio_browser` esistente: intercettazione passiva di `web_profile_info` + fallback fetch in-page. **No DOM scraping.** |
| Timing umano | `human_profile_pause` (5-10s + 12% distrazione) tra profili + micro-scroll 4-5s su ~35% dei profili + pausa lunga anti-block per-sessione. |
| Cap giornaliero | Nessun cap di default (il browser non tocca il cap API). Leva opzionale `bio_browser_daily_limit` per prudenza futura. |
| Headless | `bio_browser_headless` default `False` per il test (browser visibile); `True` in produzione. |

## 3. Contesto: cosa esiste già

Metà dell'infrastruttura è già scritta e collaudata:

- [`browser_bio.py`](../../../backend/app/services/browser_bio.py):
  - `fetch_and_store_bio_browser(follower, campaign, db, browser_session)` — estrae e scrive **gli stessi campi** Follower + `upsert_lead` della versione API. Outcome: `done` \| `private` \| `not_found` \| `soft_block` \| `network` \| `error`.
  - `_capture_web_profile_info` — cattura passiva del JSON + fallback fetch in-page.
  - `human_profile_pause` — ritmo umano tra profili.
  - `run_pause_browser_all_accounts` — fan-out `asyncio.gather` con stagger + semaforo `max_concurrent_browsers`. **È lo scheletro del parallelismo.**
  - `_scraping_accounts_of_campaign` — account scraping/both attivi della campagna.
- [`follower.py`](../../../backend/app/models/follower.py): `locked_by_account_id` + `locked_at`, optimistic lock multi-worker con rilascio stale via cron.
- [`instagram_page.py`](../../../backend/app/browser/instagram_page.py): `_simulate_browsing` / scroll — base per il micro-scroll leggero.
- [`context_manager.py`](../../../backend/app/browser/context_manager.py): `BrowserSession` (apertura, `ensure_logged_in`, coerenza proxy per-account).

Il nuovo codice **orchestra** questi mattoni; non reinventa l'estrazione.

## 4. Come vengono catturati i dati (e cosa nota Instagram)

Non è scraping del DOM. Instagram web è una SPA React: navigando su `/<username>/`, è **il JS di Instagram stesso** a chiamare il proprio endpoint interno `web_profile_info`, che restituisce un JSON con tutti i dati profilo (bio, follower/following count, email/phone business, link).

**Meccanismo 1 — intercettazione passiva (normale).** Un listener sulle `response` del browser legge quel JSON dal buffer di rete lato client. La richiesta l'ha fatta Instagram per rendere la pagina; noi la leggiamo dentro Chromium. Dal lato server **Instagram registra solo il caricamento della pagina** — identico a un utente umano che apre il profilo. La lettura non viaggia in rete: IG non la vede.

**Meccanismo 2 — fallback fetch in-page.** Se entro ~8s non abbiamo colto il JSON passivo, `page.evaluate` fa **una** richiesta a `/api/v1/users/web_profile_info/` con header `x-ig-app-id`, `credentials: 'include'`. Parte dal contesto della pagina: stessa sessione, cookie, TLS/fingerprint, referer della navigazione. È la **stessa richiesta** che il web di IG fa da sé — non la firma mobile-privata di instagrapi.

**Differenza col checkpoint dell'API:** instagrapi colpisce l'endpoint **mobile privato** (`user_info`) da device sintetico senza navigazione app-like → firma da automazione. Qui: browser reale, endpoint **web**, header/cookie/TLS del browser, referer della navigazione. Il segnale "automazione" sparisce.

**Vettore residuo:** risolto il *come* si estrae (invisibile), resta il *quanto/quanto veloce si naviga*. Aprire N profili di fila è un pattern comportamentale anche da browser vero. Lo diluiscono pause, timing random, micro-scroll e multi-account con stagger (§7).

## 5. Modello dati e migration

Nuovo campo su `Campaign`:

```python
# 'api' = Fase Bio via instagrapi (user_info, veloce, consuma cap). Default, retrocompat.
# 'browser' = Fase Bio via Patchright (web_profile_info, prudente, no cap API).
bio_engine: Mapped[str] = mapped_column(
    String(10), nullable=False, default='api', server_default='api'
)
```

Migration additiva (stile 020/`inbox_engine`): `ALTER TABLE campaigns ADD COLUMN bio_engine VARCHAR(10) NOT NULL DEFAULT 'api'`. Le campagne esistenti restano su `api`. Nessun backfill.

## 6. Architettura backend

> **Nota di revisione (2026-07-06):** l'ipotesi iniziale — un `asyncio.gather` di N sessioni dentro UN task ARQ — **non regge** contro `job_timeout = 3600s` ([task_queue.py:331](../../../backend/app/workers/task_queue.py#L331)): scrapare ~300 profili a ~15s l'uno = ~75 min > 60 min, il task verrebbe ucciso a metà, **anche senza pausa lunga**. E con `gather` non si può fare micro-yield / `Retry(defer)` senza abbattere tutte le sessioni browser aperte. L'architettura corretta è **1 task ARQ per account**, esattamente come i DM (mini-sessione → chiudi → `Retry(defer)`). Sezioni riscritte di conseguenza.

### 6.1 Biforcazione in cima a `scrape_bios`

In [`scrape_bios.py`](../../../backend/app/services/scrape_bios.py), subito dopo i guard di stato/halt esistenti:

```python
if campaign.bio_engine == 'browser':
    await enqueue_browser_bio_workers(campaign_id)  # fan-out N task ARQ, uno per account
    return None                                     # il lavoro prosegue nei task per-account
# ... resto invariato: path API con ScrapingPool ...
```

Il path API (ScrapingPool, gestione `capped`/`challenge`/`soft_block`, micro-yield) **non viene toccato**. `scrape_bios` resta l'entry point; nel ramo browser fa solo il fan-out e ritorna (non è lui a scrapare).

### 6.2 `enqueue_browser_bio_workers(campaign_id)` — fan-out per-account

Nuova funzione in `browser_bio.py`. Enqueue di un task ARQ **per account** scraping, con **stagger via defer iniziale** (nessun `gather`, nessun blocco):

```
enqueue_browser_bio_workers(campaign_id):
    guard stato/halt/target
    accounts = _scraping_accounts_of_campaign(campaign_id)
    se nessun account → status=error, evento, return
    per idx, (account_id, _) in accounts:
        _job_id deterministico = f"biobrowser:{campaign_id}:{account_id}"   # dedup ARQ (come i DM)
        defer = random(stagger_min, stagger_max) * idx                      # partenza differita
        arq.enqueue_job("browser_bio_account_task", campaign_id, account_id, _defer_by=defer, _job_id=_job_id)
```

Il `_job_id` deterministico impedisce due task concorrenti per lo stesso (campagna, account) — stesso pattern anti-duplicazione dei worker DM.

### 6.3 `browser_bio_account_task` — mini-sessione per-account (nuovo worker)

Nuovo task ARQ (in `task_queue.py` o worker dedicato) + funzione `scrape_bios_browser_session(campaign_id, account_id)` in `browser_bio.py`. **Job corto** = una mini-sessione, poi defer. Sopravvive a `job_timeout` perché ogni job dura al più `session_cap × ~15s` (« 3600s):

```
browser_bio_account_task(ctx, campaign_id, account_id):
    defer = await scrape_bios_browser_session(campaign_id, account_id)
    if defer: raise Retry(defer=defer)     # pausa lunga anti-block → re-fire dopo N min
    # else: pool esaurito / target raggiunto / stop → fine

scrape_bios_browser_session(campaign_id, account_id):
    guard stato/halt/target
    session = BrowserSession(account_id, headless=bio_browser_headless)
    session.open(); session.page.ensure_logged_in(account_id)
    cap = pick_session_cap(...)            # quanti profili in QUESTA mini-sessione
    processed = 0
    try:
        while processed < cap and bio_should_continue(target, done_globale):
            se halt → return None
            follower = claim_next_pending(campaign_id, account_id)   # §6.4 atomico
            se None → return None                                    # pool esaurito
            outcome, err = fetch_and_store_bio_browser(follower, campaign, db, session)
            gestisci outcome (§8)          # done: rilascia lock + bio_scraped
            maybe_micro_scroll(session)    # ~35%, 4-5s
            human_profile_pause()
            processed += 1
    finally:
        session.close()                    # chiudi sempre: il defer termina il task
    return random(scrape_break_minutes_min, max) * 60   # pausa lunga → Retry(defer)
```

**Login per mini-sessione, non per profilo.** Un login ogni `session_cap` profili (~20-40) è realistico: un umano apre l'app, guarda un blocco di profili, chiude, riapre dopo. Il browser resta aperto per l'intera mini-sessione, si chiude prima del defer.

Ogni task apre la **propria** `AsyncSessionLocal()` (sessioni SQLAlchemy async non concorrenti-safe — vincolo già rispettato in `run_pause_browser_activity`). `max_concurrent_browsers`: con task ARQ separati il cap non è più un semaforo in-process; va rispettato limitando `max_jobs` del worker o via un semaforo Redis/gate leggero (§13).

### 6.4 Claim atomico dei pending (pool disgiunti)

La selezione `SELECT ... pending LIMIT 1` di `_scrape_batch` **non è concorrenza-safe**: due sessioni parallele prenderebbero lo stesso follower. Sostituita da un claim atomico che riusa i campi lock esistenti:

```sql
UPDATE followers
   SET locked_by_account_id = :acc, locked_at = now()
 WHERE id = (
    SELECT id FROM followers
     WHERE campaign_id = :cid AND status = 'pending' AND locked_by_account_id IS NULL
     LIMIT 1
     FOR UPDATE SKIP LOCKED           -- Postgres; su SQLite dev: optimistic UPDATE+rowcount
 )
RETURNING id;
```

- **Postgres (prod):** `FOR UPDATE SKIP LOCKED` → assegnazione senza contesa.
- **SQLite (dev):** `UPDATE ... WHERE locked_by IS NULL` con controllo `rowcount` (stesso pattern optimistic-lock già usato per i DM).
- **Release stale inline:** `claim_next_pending` rilascia prima i lock stale della campagna (`locked_at < now - LOCK_TIMEOUT_MINUTES`), identico al claim DM ([campaign_orchestrator.py:1008-1019](../../../backend/app/services/campaign_orchestrator.py#L1008)). Così un profilo preso da una sessione morta torna claimabile senza attendere il cron.

> **⚠️ C2 — rilascio lock al passaggio a `bio_scraped` (bug se omesso).** Il claim della **fase DM** cerca `status IN (bio_scraped, message_generated) AND locked_by_account_id IS NULL` ([campaign_orchestrator.py:1027](../../../backend/app/services/campaign_orchestrator.py#L1027)). Se scrapo la bio e passo il follower a `bio_scraped` **lasciando il lock del claim bio valorizzato**, quel follower resta invisibile alla fase DM fino allo stale-timeout (20 min). Quindi su outcome `done` va fatto **`locked_by_account_id = None, locked_at = None`** insieme a `status = bio_scraped`, nello stesso commit. Idem su `skipped` (pulizia). `fetch_and_store_bio_browser` va modificata per azzerare il lock (oggi non lo fa perché il claim atomico non esisteva).

> **✅ C3 — cron stale-lock già globale (era punto aperto).** [`release_stale_locks`](../../../backend/app/services/campaign_orchestrator.py#L1107) gira via cron ogni 15 min ([cron_worker.py:21](../../../backend/app/workers/cron_worker.py#L21)) su **tutti** i Follower con lock > `LOCK_TIMEOUT_MINUTES`, indipendentemente da fase/status. Copre già i lock di scraping bio: safety net garantito anche senza il release inline.

> **✅ C4 — nessuna collisione lock bio ↔ DM.** Verificato: il lock bio agisce su `status = pending`, il lock DM su `bio_scraped`/`message_generated`. Set di follower **disgiunti per status** → il campo `locked_by_account_id` è condiviso ma i record lockati non si sovrappongono mai, nemmeno in `scraping_and_running`. Unico accorgimento: C2 sopra (rilasciare al passaggio di stato).

### 6.5 Micro-scroll umano

`maybe_micro_scroll(session)`: con probabilità `bio_browser_scroll_ratio` (default 0.35), dopo la cattura dati fa uno scroll leggero di `bio_browser_scroll_min_s`–`max_s` (4-5s) sulla pagina profilo già aperta, poi prosegue. Estratto/semplificato da `_simulate_browsing`. Non su tutti i profili: la costanza è essa stessa una firma.

## 7. Timing e ritmo (riepilogo)

| Livello | Meccanismo | Valore |
|---|---|---|
| Tra profili | `human_profile_pause` | 5-10s, +12% distrazione 15-45s |
| Su ~35% profili | micro-scroll | 4-5s |
| Ogni `scrape_session_size` profili/sessione | pausa lunga anti-block | `scrape_break_minutes_min/max` (30-45 min) |
| Prima apertura per account | stagger via `_defer_by` all'enqueue | 60-180s × indice account |
| Browser concorrenti | `max_jobs` worker / gate Redis (non più semaforo in-process) | ≤ `max_concurrent_browsers` |

Stima per profilo: ~10-18s mediana, occasionale ~50s (distrazione). Ampiamente sotto i 30s/profilo richiesti.

## 8. Gestione outcome ed errori

`fetch_and_store_bio_browser` ritorna `(outcome, err)`:

| Outcome | Azione nel worker |
|---|---|
| `done` | `done += 1`, reset contatori, `human_profile_pause`, eventuale pausa lunga al session-cap |
| `not_found` / `private` / `error` | `status = skipped`, `skip_reason = browser_<outcome>`, avanti (non ri-seleziona: lo status esce da pending) |
| `soft_block` (429/401/403) | **stop di questa sessione**: i pending claimati NON ancora scritti tornano `pending` (rilascio lock), evento warn. Le altre sessioni proseguono. N soft_block consecutivi globali → pausa campagna. |
| `network` | stop sessione, preserva i pending (come API). Se tutte le sessioni cadono per rete → `status=error`, evento. |

Contatori consecutivi (`consecutive_fail`, `consecutive_soft`) come nel path API, ma **per mini-sessione** più una soglia globale per pausare la campagna. Eventi frontend (`emit`) riusati: `scrape_start`, `scrape_progress`, `scrape_break`, `scrape_stopped`.

Difensività: `browser_bio_account_task` avvolge la mini-sessione in try/except (come `bio_worker.scrape_bios_task`): un errore DB/rete transitorio → `Retry(defer)`, un errore inatteso logga e termina il task **di quell'account** senza toccare gli altri (task ARQ indipendenti). `session.close()` in `finally` sempre.

## 9. Config nuova (`config.py`)

```python
bio_browser_headless: bool = False        # test: visibile; prod: True
bio_browser_scroll_ratio: float = 0.35    # frazione profili con micro-scroll
bio_browser_scroll_min_s: float = 4.0
bio_browser_scroll_max_s: float = 5.0
bio_browser_daily_limit: int | None = None  # cap opzionale profili/account/giorno (None = off)
bio_browser_stagger_min_s: float = 60.0   # differita prima apertura per account
bio_browser_stagger_max_s: float = 180.0
```

Riusati esistenti: `max_concurrent_browsers`, `scrape_session_size`, `scrape_break_minutes_min/max`.

## 10. Frontend

Form creazione/modifica campagna: dropdown **"Motore Fase Bio"** con `API (veloce)` / `Browser (prudente)`, mappato su `bio_engine`. Default `API`. Il campo viaggia nello schema `CampaignCreate`/`CampaignUpdate` e `CampaignResponse`. Nessun'altra modifica UI per il test (headless configurato via `.env`).

## 11. Cosa NON si tocca

- Path API di `scrape_bios` (ScrapingPool, gestione capped/challenge, micro-yield).
- `fetch_and_store_bio` (API) e i suoi guardrail.
- Schema/logica di Fase Lista, generazione AI, invio DM.
- Il flusso `run_pause_browser_all_accounts` durante la pausa (resta attivo per il motore API; convivenza da verificare — vedi §13).

## 12. Testing

1. **Unit puri (no IO):** claim atomico (mock DB rowcount), decisione micro-scroll (ratio deterministica via seed di prova), mappatura outcome → azione.
2. **Integrazione locale:** campagna `bio_engine=browser`, 1 account reale, ~10 profili pending, `headless=False`. Verificare: dati scritti identici al path API, ritmo, nessun consumo cap API.
3. **Parallelo:** 2 account, verificare pool disgiunti (nessun follower scrapato due volte — query di controllo su `locked_by_account_id`) e stagger effettivo.
4. **Regressione:** una campagna `bio_engine=api` gira esattamente come prima (path invariato).

## 13. Punti aperti (da chiudere in fase di piano o dopo il test)

- **✅ Pausa lunga (risolto):** modello 1-task-per-account (§6.3), pausa lunga = `Retry(defer)` nativo ARQ tra mini-sessioni. Ogni job resta corto → nessun rischio `job_timeout`. Chiude l'ipotesi gather/sleep-in-session scartata.
- **✅ Cron stale-lock (risolto → C3):** `release_stale_locks` è globale su tutti i Follower, copre già i lock bio.
- **`max_concurrent_browsers` con task ARQ separati.** Non è più un semaforo in-process. Per il test: limitare `max_jobs` del worker (o usare un worker dedicato con `max_jobs = max_concurrent_browsers`). Per produzione valutare un gate leggero su Redis (chiave-contatore) se serve un cap trasversale a più campagne. Decisione in fase di piano.
- **Worker: riuso `WorkerSettings` esistente vs dedicato.** `browser_bio_account_task` può stare nel worker esistente (`job_timeout=3600` basta, i job sono corti) oppure in un worker dedicato con `max_jobs` calibrato sui browser. Raccomandazione: **riuso del worker esistente** per il test (meno moving parts), worker dedicato solo se il cap browser lo richiede.
- **Convivenza con l'attività browser in pausa del motore API** (`run_pause_browser_all_accounts` in `scrape_bios.py:279`): quando `bio_engine=browser` quella non deve girare (doppione). **Gate:** eseguirla solo se `campaign.bio_engine != 'browser'`.
- **Alternanza/ibrido API+browser:** esplicitamente fuori scope. Decisione dopo il test sul campo.

### 13.1 Note operative per il test (non bloccanti)

- **C5 — account contesi:** il [mutex per-account](../../../backend/app/browser/context_manager.py#L23) serializza le aperture browser per lo stesso account. Un account `role=both` conteso tra invio DM e bio-browser si strozza. Per il test usare account **solo-scraping** o campagna con `messaging_enabled=False`.
- **C6 — risorse locali:** con `bio_browser_headless=False` ogni account = una finestra Chromium. Dato il disco C: piccolo + RAM del PC, tenere `max_concurrent_browsers`/`max_jobs` a **1-2** durante il test visibile.

## 14. File toccati (previsione)

| File | Modifica |
|---|---|
| `backend/app/models/campaign.py` | campo `bio_engine` |
| `backend/app/database.py` (o migration inline) | migration additiva colonna |
| `backend/app/services/scrape_bios.py` | biforcazione in cima (fan-out) + gate `run_pause_browser_all_accounts` su `bio_engine != browser` |
| `backend/app/services/browser_bio.py` | `enqueue_browser_bio_workers`, `scrape_bios_browser_session`, `claim_next_pending`, `maybe_micro_scroll`; modifica `fetch_and_store_bio_browser` per azzerare il lock su `done`/`skipped` (C2) |
| `backend/app/workers/task_queue.py` (o bio_worker) | nuovo task `browser_bio_account_task` registrato in `WorkerSettings.functions` |
| `backend/app/config.py` | config nuova §9 |
| `backend/app/schemas/campaign.py` | `bio_engine` in Create/Update/Response |
| `frontend/` (form campagna) | dropdown motore Fase Bio |
| `backend/tests/` | test §12 |
| `docs/project/PROGRESS.md`, `INDEX.md`, `CLAUDE.md` | aggiornamento contesto a fine implementazione |
