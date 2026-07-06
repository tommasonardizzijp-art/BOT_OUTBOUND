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
| Approccio d'innesto | **A — funzione browser dedicata**. `scrape_bios()` biforca in cima; se `browser` delega a `scrape_bios_browser()`. Path API **intatto** (zero rischio regressione). No refactor engine-agnostic per ora. |
| Parallelismo | **Multi-account in parallelo**, con partenze **differite** (stagger 1-3 min crescente). Ogni account macina un **pool disgiunto** di pending. |
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

### 6.1 Biforcazione in cima a `scrape_bios`

In [`scrape_bios.py`](../../../backend/app/services/scrape_bios.py), subito dopo i guard di stato/halt esistenti:

```python
if campaign.bio_engine == 'browser':
    return await scrape_bios_browser(campaign_id)
# ... resto invariato: path API con ScrapingPool ...
```

Il path API (ScrapingPool, gestione `capped`/`challenge`/`soft_block`, micro-yield) **non viene toccato**. Il worker [`bio_worker.py`](../../../backend/app/workers/bio_worker.py) resta invariato: chiama `scrape_bios`, che ora può ritornare il defer anche dal path browser.

### 6.2 `scrape_bios_browser(campaign_id)` — orchestratore parallelo

Nuova funzione in `browser_bio.py`. Riusa la forma di `run_pause_browser_all_accounts`:

```
scrape_bios_browser(campaign_id):
    guard stato/halt/target (come path API)
    accounts = _scraping_accounts_of_campaign(campaign_id)
    se nessun account → status=error, evento, return
    sem = Semaphore(max_concurrent_browsers)
    gather(
        _browser_account_worker(campaign_id, acc, idx) for idx, acc in accounts
    )  # return_exceptions=True: un account che cade non blocca gli altri
    aggiorna esito campagna (completed/partial) come il path API
```

### 6.3 `_browser_account_worker(campaign_id, account, idx)` — sessione singola

Un account = una `BrowserSession` loggata, tenuta aperta, che cicla i pending fino a esaurimento/target/pausa:

```
_browser_account_worker:
    se idx: sleep(stagger 60-180s * idx)          # partenza differita
    async with sem:
        session = BrowserSession(account_id, headless=bio_browser_headless)
        session.open(); session.page.ensure_logged_in(account_id)
        loop:
            se halt → break
            follower = claim_next_pending(campaign_id, account_id)   # §6.4 atomico
            se None → break                        # pool esaurito (globale)
            outcome, err = fetch_and_store_bio_browser(follower, campaign, db, session)
            gestisci outcome (§8)
            maybe_micro_scroll(session)            # ~35%, 4-5s
            human_profile_pause()
            se raggiunto session-cap → pausa lunga anti-block (defer o sleep in-session)
        session.close()
```

Ogni worker apre la **propria** `AsyncSessionLocal()` (le sessioni SQLAlchemy async non sono concorrenti-safe — stesso vincolo già rispettato in `run_pause_browser_activity`).

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
- A `done`/`skipped` lo status esce da `pending`: il lock non serve più liberarlo esplicitamente, ma i **stale lock** (profilo preso e sessione morta prima di scriverlo) restano `pending` con lock vecchio → li rilascia il cron esistente (`LOCK_TIMEOUT_MINUTES`). Verificare che il cron di rilascio stale copra anche i lock di scraping bio, non solo DM.

### 6.5 Micro-scroll umano

`maybe_micro_scroll(session)`: con probabilità `bio_browser_scroll_ratio` (default 0.35), dopo la cattura dati fa uno scroll leggero di `bio_browser_scroll_min_s`–`max_s` (4-5s) sulla pagina profilo già aperta, poi prosegue. Estratto/semplificato da `_simulate_browsing`. Non su tutti i profili: la costanza è essa stessa una firma.

## 7. Timing e ritmo (riepilogo)

| Livello | Meccanismo | Valore |
|---|---|---|
| Tra profili | `human_profile_pause` | 5-10s, +12% distrazione 15-45s |
| Su ~35% profili | micro-scroll | 4-5s |
| Ogni `scrape_session_size` profili/sessione | pausa lunga anti-block | `scrape_break_minutes_min/max` (30-45 min) |
| Prima apertura per account | stagger | 60-180s × indice account |
| Browser concorrenti | semaforo | `max_concurrent_browsers` |

Stima per profilo: ~10-18s mediana, occasionale ~50s (distrazione). Ampiamente sotto i 30s/profilo richiesti.

## 8. Gestione outcome ed errori

`fetch_and_store_bio_browser` ritorna `(outcome, err)`:

| Outcome | Azione nel worker |
|---|---|
| `done` | `done += 1`, reset contatori, `human_profile_pause`, eventuale pausa lunga al session-cap |
| `not_found` / `private` / `error` | `status = skipped`, `skip_reason = browser_<outcome>`, avanti (non ri-seleziona: lo status esce da pending) |
| `soft_block` (429/401/403) | **stop di questa sessione**: i pending claimati NON ancora scritti tornano `pending` (rilascio lock), evento warn. Le altre sessioni proseguono. N soft_block consecutivi globali → pausa campagna. |
| `network` | stop sessione, preserva i pending (come API). Se tutte le sessioni cadono per rete → `status=error`, evento. |

Contatori consecutivi (`consecutive_fail`, `consecutive_soft`) come nel path API, ma **per-sessione** più una soglia globale per pausare la campagna. Eventi frontend (`emit`) riusati: `scrape_start`, `scrape_progress`, `scrape_break`, `scrape_stopped`.

Difensività: un'eccezione inattesa in una sessione è ingoiata e logga (come `run_pause_browser_activity`), la `gather` ha `return_exceptions=True`.

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

- **Pausa lunga: defer ARQ vs sleep in-session.** Con multi-account in un solo task `gather`, un `Retry(defer=)` a livello di task ferma *tutte* le sessioni. Opzioni: (a) pausa lunga come `sleep` in-session per-account (semplice, ma tiene il task ARQ vivo a lungo → attenzione a `job_timeout`); (b) segmentare in job più corti come fa il path API. Per il test parte (a); rivalutare per volumi.
- **Convivenza con l'attività browser in pausa del motore API** (`run_pause_browser_all_accounts`): quando `bio_engine=browser` quella non deve girare (sarebbe doppione). Gate sul motore.
- **Cron stale-lock:** confermare che rilasci i lock di scraping bio e non solo quelli DM.
- **Alternanza/ibrido API+browser:** esplicitamente fuori scope. Decisione dopo il test sul campo.

## 14. File toccati (previsione)

| File | Modifica |
|---|---|
| `backend/app/models/campaign.py` | campo `bio_engine` |
| `backend/app/database.py` (o migration inline) | migration additiva colonna |
| `backend/app/services/scrape_bios.py` | biforcazione in cima |
| `backend/app/services/browser_bio.py` | `scrape_bios_browser`, `_browser_account_worker`, `claim_next_pending`, `maybe_micro_scroll` |
| `backend/app/config.py` | config nuova §9 |
| `backend/app/schemas/campaign.py` | `bio_engine` in Create/Update/Response |
| `frontend/` (form campagna) | dropdown motore Fase Bio |
| `backend/tests/` | test §12 |
| `docs/project/PROGRESS.md`, `INDEX.md`, `CLAUDE.md` | aggiornamento contesto a fine implementazione |
