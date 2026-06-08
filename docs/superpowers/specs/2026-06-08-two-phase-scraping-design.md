# Design — Scraping a due fasi (Lista / Bio separate)

> Branch: `feature/advanced-scraping` · Data: 2026-06-08 · `source_type=scrape`

## Problema

Lo scraping attuale (`source_type=scrape`) intreccia in un unico loop l'estrazione della
lista follower e l'estrazione bio/contatti per ogni follower. Due conseguenze:

1. **Burst lista**: `user_followers_v1_chunk` viene chiamato con `max_amount=0` →
   il loop interno di instagrapi drena l'INTERA lista (count=200 per richiesta,
   back-to-back, **senza delay**) prima di ritornare. Su liste 9k+ è una raffica di
   ~45 richieste a `/friendships/{pk}/followers/` → challenge IG "comportamento
   automatizzato". È anche la lunga fase "non succede niente" osservata dall'utente.
2. **Bio costosa e bloccante**: subito dopo, per OGNI follower parte `user_info_v1`
   (endpoint `/users/{pk}/info/`, capped 180/giorno/account, principale causa di
   soft-block). Non si può estrarre solo la lista, né controllare le due cose
   separatamente.

## Obiettivo

Separare lo scraping `source_type=scrape` in **due fasi indipendenti**, ognuna con
avvio/stop/target/ripresa propri:

- **Fase Lista**: raccoglie solo info base, paced a blocchetti piccoli randomizzati. Veloce, leggera, non tocca il cap.
- **Fase Bio**: estrae bio+contatti dai follower già in lista, uno per uno, sotto cap.

Permette l'**interleaving**: estrai lista → avvia bio → ferma bio → estrai altra lista
→ riprendi bio. L'import (`source_type=import`) e il flusso DM a valle restano invariati.

Fuori scope v1 (rimandato): parallelo simultaneo lista+bio su account diversi.

## Modello a due fasi e stati

**Macchina a stati follower (riuso, nessun nuovo enum):**
- Fase Lista crea `Follower(status=pending)` con solo info base dalla chiamata lista
  (username, full_name, profile_pic_url, is_private, is_verified). **Nessuna
  `user_info_v1`.**
- Fase Bio prende i `Follower(status=pending)` → `user_info_v1` → bio+contatti →
  `status=bio_scraped`.

**Nuovi stati campagna** (`CampaignStatus`, enum stringa `native_enum=False` → nessuna
migrazione di tipo DB necessaria, solo valori nuovi):
- `listing`: Fase Lista attiva
- `listing_break`: pausa paced della Fase Lista (countdown UI)
- `scraping`: Fase Bio attiva (riuso esistente; ora opera sui `pending` dal DB, non dal live)
- `scraping_break`: pausa sessione bio (esistente)
- `ready`: almeno qualcosa di scrapato, pronta per DM/export
- `paused` / `error` / `completed`: come ora

Vincolo v1: **una sola fase attiva per volta** per campagna (no parallelo). Questo
permette di riusare i campi di break esistenti.

**Progressi distinti** (derivati da query sui follower + target persistiti):
- Lista: `count(Follower) / list_target` (es. 340/500; target vuoto = tutta la lista IG)
- Bio: `count(status=bio_scraped) / count(status in (pending, bio_scraped))` (es. 120/340)

## Modifiche dati (migrazione minima)

Nuove colonne su `campaigns`:
- `list_target: int | None` — obiettivo Fase Lista (NULL = tutta la lista)
- `bio_target: int | None` — obiettivo Fase Bio (NULL = tutti i pending)

Riuso esistenti:
- `scrape_cursor`: cursore di paginazione della **Fase Lista** (max_id IG)
- `scrape_break_until` + `scrape_break_prev_status`: usati sia da `listing_break` sia da
  `scraping_break` (lecito perché le fasi non sono mai simultanee in v1)
- `scrape_session_size`, `scrape_break_minutes_min/max`, `scrape_daily_limit`,
  `scrape_outcome`: invariati, usati dalla Fase Bio

Nessun nuovo enum DB (status è stringa). Migrazione = 2 `ADD COLUMN` nullable.

## Fase Lista — meccanica

- Paginazione a blocchetti: `max_amount = random(20, 40)` per pagina (passato a
  `user_followers_v1_chunk` / `user_following_v1_chunk` → loop interno si ferma a N).
- Delay tra pagine: **5-10s lognormale** (non uniforme).
- Pausa lunga occasionale (~ogni 15-20 pagine, 30-60s) = scroll che si ferma.
- `scrape_cursor` salvato dopo ogni pagina → ripresa esatta.
- Crea `Follower(status=pending)` con info base; dedup su `(campaign_id, ig_user_id)`
  esistente.
- **Non consuma il cap 180** (endpoint lista ≠ `user_info_v1`).
- Termina quando: raggiunge `list_target`, OPPURE lista IG esaurita (`max_id` None),
  OPPURE stop manuale.
- Su challenge/soft-block: isola l'account (`challenge_required`) e mette la campagna in
  `paused` (handler già introdotto in `scraper.py` questa sessione, da riusare).

Timing: 500 ≈ ~2 min; 9000 ≈ ~30-35 min.

## Fase Bio — meccanica

- Legge `Follower(status=pending)` della campagna dal DB.
- Per ognuno: `user_info_v1(pk)` → bio, follower/following count, external_url,
  telefono/email/whatsapp via `extract_contacts` → `status=bio_scraped`.
- Upsert in `global_contacts` (lead DB) come ora.
- Delay tra follower: `bio_fetch_delay` 5-8s (esistente).
- Round-robin sul `ScrapingPool`, rotazione su 429/soft-block (esistente).
- **Consuma il cap** (default alzato a 300/giorno/account, override per-campagna su
  `scrape_daily_limit`).
- Pausa sessione ogni `scrape_session_size` → `scraping_break` (esistente).
- Termina quando: raggiunge `bio_target`, OPPURE finiti i `pending`, OPPURE stop
  manuale, OPPURE cap raggiunto (`scrape_capped`, esistente).
- Ripresa: riparte dai `pending` rimasti (stato nel DB, non in memoria).

Timing: 200 bio ≈ ~20 min (sotto cap).

## Interleaving

Le due fasi sono job ARQ separati con stop/resume propri e stato persistito nel DB.
Sequenza supportata: lista(500) → bio(start) → bio(stop a 120) → lista(+500) →
bio(resume) — i nuovi `pending` entrano nella coda bio automaticamente. Funziona perché
entrambe leggono lo stato dal DB.

## Architettura tecnica

**Due job ARQ** (riuso pattern `scrape:`/`resolve:`):
- `list_followers_task(campaign_id)` — Fase Lista. Job id `list:{campaign_id}`.
- `scrape_bios_task(campaign_id)` — Fase Bio. Job id `bios:{campaign_id}`.

**Refactor `scraper.py`** (oggi monolitico):
- Estrarre la paginazione lista in una funzione/modulo Fase Lista (senza bio).
- Estrarre il loop bio (oggi in `_store_followers_batch`) in una funzione Fase Bio
  riusabile che cicla sui `pending` dal DB.
- `ScrapingPool`, gestione challenge/soft-block, session break, cap: condivisi/riusati.
- Il vecchio `scrape_followers` (interleaved) resta temporaneamente per non rompere job
  in volo; il flusso UI nuovo usa i due job separati. Deprecazione successiva.

**Endpoint** (estendono i pattern esistenti in `api/campaigns.py`):
- `POST /campaigns/{id}/list/start` (body opz. `{target}`) → `listing`
- `POST /campaigns/{id}/list/stop` → `paused`
- `POST /campaigns/{id}/bios/start` (body opz. `{target}`) → `scraping`
- `POST /campaigns/{id}/bios/stop` → `paused`
- `GET /campaigns/{id}`: response estesa con `list_progress` e `bio_progress`.
- Guardie: account scraping/both attivo, Redis raggiungibile, kill-switch (riuso helper
  esistenti `has_active_role_account`, `_check_redis_reachable`, `ensure_bot_accepts_work`).
- `start-scrape` legacy: per `source_type=scrape` reindirizza alla Fase Lista; import
  invariato (`enqueue_resolve`).

**Frontend** (`frontend/app/campaigns/[id]` o equivalente):
- Due pannelli espliciti: "Fase 1 — Lista follower" e "Fase 2 — Scraping bio/contatti".
- Ognuno: campo target, bottone Avvia/Stop, progress bar + stato (con countdown break).

**Config** (`config.py`):
- `scrape_daily_limit` default 180 → **300**.
- Nuove: `list_page_size_min=20`, `list_page_size_max=40`, `list_page_delay_min=5`,
  `list_page_delay_max=10`, `list_long_pause_probability`, `list_long_pause_min/max`.
  (Le `scrape_page_*` introdotte prima questa sessione vengono riallineate/rinominate per
  la Fase Lista.)

## Backward compatibility

- `source_type=import`: invariato (resolve già parte da lista fornita).
- DM/messaging a valle: invariato (parte dai `bio_scraped`).
- Campagne esistenti `ready`/`completed`: non toccate.
- Cron recovery/startup-guard/stale-lock: estesi per riconoscere `listing`/`listing_break`
  come stati attivi (altrimenti lo startup-guard li pauserebbe come stale).

## Rischi e mitigazioni

- **Complessità di stato (medio)**: due progressi + due pause. Mitigato da contatori
  derivati da query DB (single source of truth) e una sola fase attiva per volta.
- **Lista comunque ~270 chiamate/9k (basso)**: paced 5-10s + pausa lunga occasionale +
  proxy obbligatorio. Molto meglio della raffica, non zero.
- **Cap 300 più aggressivo (scelta utente)**: rischio ban maggiore vs 180; accettato,
  override per-campagna disponibile.
- **Refactor scraper.py (medio)**: il monolite va spezzato con cura per non perdere
  guardrail (kill-switch, challenge handler, session break, cursore). Mitigato tenendo il
  vecchio path finché i due nuovi sono testati.

## Fuori scope (rimandato)

- Parallelo simultaneo lista+bio su account diversi (Parte D). Da valutare come fase 2
  quando il flusso a due fasi è solido.
