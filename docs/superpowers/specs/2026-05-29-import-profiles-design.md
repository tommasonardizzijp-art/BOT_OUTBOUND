# Import profili da lista — Design

> Branch: `feature/import-profiles` · Data: 2026-05-29

## Obiettivo

Permettere a una campagna di partire da una **lista di profili Instagram fornita dall'utente** (URL/username caricati da file) invece che dallo scraping di follower/following di una pagina target. Caso d'uso: il cliente ha già selezionato qualche centinaio di profili e vuole contattarli direttamente, senza dover far seguire i profili da un account per poi scrapparne il following.

La bio per la personalizzazione del messaggio viene comunque recuperata via account Instagram (instagrapi), riusando l'anti-detection esistente. Il flusso di generazione AI e invio DM a valle resta **invariato**.

## Decisioni prese (brainstorming)

| Tema | Decisione |
|---|---|
| Sorgente bio | Fetch via account IG (riuso flusso esistente, `user_info_by_username_v1` = 1 call → pk + bio) |
| Formato input | Upload file `.txt` / `.csv` |
| Modello dati | Nuovo campo `source_type` su `campaigns` (`scrape` \| `import`) |
| Timing import | Solo alla creazione della campagna |
| Parsing file | 1 valore per riga (URL o username); su CSV prende la prima colonna |
| Profilo privato | Crea comunque il `Follower` (`bio_scraped`) e tenta il DM — coerente con lo scraper attuale |

## Approccio scelto

**Staging table + resolve worker dedicato.** Motivazione: `Follower.ig_user_id` è `NOT NULL` + unique `(campaign_id, ig_user_id)`; all'import si ha solo lo username, il `pk` arriva dopo la risoluzione IG. Serve quindi uno stadio intermedio prima di creare i `Follower`. Una tabella di staging dedicata isola la responsabilità, riusa l'anti-detection dello scraper e dà osservabilità per-profilo in UI (risolti / non trovati / privati / errore).

Scartati: branch dentro `scrape_followers()` (raddoppia la complessità di una funzione già densa); lista JSON su `campaigns` (zero osservabilità, blob ingombrante per centinaia/migliaia di righe).

## Modello dati

### `campaigns` — nuovo campo
- `source_type: str` — `'scrape'` (default) | `'import'`
- `target_username` → reso **nullable** (per import non esiste pagina target). Le query/UI esistenti che lo assumono presente vanno protette con guardie `source_type == 'scrape'`.

Migrazione: `ALTER TABLE campaigns ADD COLUMN source_type ... DEFAULT 'scrape'` inline al boot in `database.py` (stesso pattern delle colonne aggiunte a `global_contacts`), così le campagne esistenti restano `scrape`.

### `imported_profiles` — nuova tabella staging
```
id            str PK (uuid)
campaign_id   FK → campaigns.id (ondelete CASCADE), index
raw_input     str         # riga originale dal file
username      str         # username estratto/normalizzato (lowercase)
status        str         # pending | resolved | not_found | private | error
ig_user_id    BigInteger? # popolato dopo resolve
error         str?        # dettaglio errore se status=error
created_at    datetime
updated_at    datetime
UniqueConstraint(campaign_id, username)  # dedup interno alla campagna
```
Stati: `pending` (da risolvere) → `resolved` (Follower creato) | `not_found` | `private` (informativo; Follower comunque creato) | `error`.

## Flusso backend

1. **Creazione campagna** con `source_type='import'`: il form non richiede `target_username` né `scrape_mode`; include l'upload file. Frontend invia i dati campagna, poi l'upload del file in un secondo step verso l'endpoint dedicato.
2. **`POST /campaigns/{id}/import-profiles`** (multipart, file `.txt`/`.csv`):
   - legge il file, per ogni riga estrae lo username (parser, vedi sotto), normalizza lowercase, ignora righe vuote
   - dedup interno + crea righe `imported_profiles(status='pending')`
   - ritorna `{ total_lines, valid, skipped_invalid, duplicates }`
   - 400 se 0 righe valide
3. **Start** (`POST /campaigns/{id}/start` o `start-dm-auto`): se `source_type='import'` → status `scraping` (riuso lo stato esistente; label UI "Risoluzione profili") → enqueue `resolve_imports_task(campaign_id)`.
4. **`resolve_imports_task(campaign_id)`** (nuovo worker, riusa helper estratti dallo scraper):
   - login di un account assegnato con ruolo `scraping`/`both` (stesso requisito odierno; stesso errore se assente)
   - itera le righe `imported_profiles` con `status='pending'`:
     - `user_info_by_username_v1(username)` con retry + rotazione account su 429/soft-block (riuso logica scraper)
     - successo → crea `Follower(status='bio_scraped')` con pk/bio/full_name/ecc. + staging `resolved` + `ig_user_id`
     - `UserNotFound` → staging `not_found`
     - profilo privato → staging `private` ma **crea comunque** il Follower
     - altro errore → staging `error` + messaggio
   - **session break** configurabile riusando `scrape_session_size`, `scrape_break_minutes_min/max`; **delay** tra call riusando `bio_fetch_delay_min/max`
   - rispetta kill-switch globale e pausa/stop campagna con check per-profilo (come lo scraper)
   - idempotente: un re-run dopo crash riprende solo le righe `pending`
   - fine: `campaign.total_followers` = count Follower; status → `ready` (o `running` se era `scraping_and_running`); emette evento `scrape_complete` equivalente
5. **DM downstream invariato**: i `Follower` in stato `bio_scraped` entrano nel flusso AI + invio esistente senza modifiche.

### Parser username
Input per riga, in ordine di tentativo:
- URL completo: `https://instagram.com/<user>/`, con o senza `www`, trailing slash, query string → estrae `<user>`
- `@<user>` → `<user>`
- `<user>` nudo
- CSV: prende la **prima colonna** della riga
- normalizzazione: lowercase, strip spazi; scarta token che non rispettano il charset username IG (`[a-z0-9._]`)

## Frontend

- **Form nuova campagna**: toggle in cima **"Sorgente: Scraping pagina | Lista importata"**.
  - `Scraping pagina` → form attuale invariato (`target_username` + `scrape_mode`).
  - `Lista importata` → nasconde `target_username` e `scrape_mode`; mostra **upload file** (.txt/.csv) con anteprima conteggio righe parse-ate lato client + nota "serve un account con ruolo scraping/both assegnato".
- **Detail campagna import**: pannello "Profili importati" con contatori per-stato (pending / resolved / not_found / private / error) via polling SWR, nello stile degli eventi live. Stati `scraping`/`scraping_break` mostrano label "Risoluzione…".
- `lib/types.ts`: campo `source_type`, tipi import. `api.ts`: funzione upload + endpoint contatori staging.

## Edge cases & errori

- File vuoto / 0 righe valide → 400 con messaggio chiaro.
- Username non risolvibile → staging `not_found`, log, prosegue.
- Profilo privato → staging `private`, Follower comunque creato (bio eventualmente vuota).
- Duplicati nel file → bloccati da `unique(campaign_id, username)`.
- Dedup `global_contacts`: NON a resolve-time. Lo `ig_user_id` è noto solo *dopo* la call IG e il worker DM già deduplica a send-time (`skip_reason="already_contacted_globally"`). Si mirrora lo scraper: si crea sempre il Follower, la dedup avviene all'invio. Niente stato `skipped`.
- Nessun account `scraping`/`both` assegnato → stesso errore esistente dello scraper; campagna resta in stato gestito (non va in `error` su retry stale).
- Crash recovery: `resolve_imports_task` riprende solo righe `pending` (idempotente).

## Testing

- **Unit parser**: URL completo / con query / trailing slash, `@handle`, bare username, riga sporca, CSV prima colonna, charset invalido.
- **Unit dedup**: interno (file) + `global_contacts`.
- **Integration (mock instagrapi)**: resolve task con esiti `not_found` / `private` / `success` → stati staging corretti + `Follower` creati nei casi attesi.
- **Migrazione**: `source_type` default `'scrape'` applicato a campagne esistenti.

## Fuori scope (YAGNI)

- Import di bio pre-compilate (CSV con colonna bio) — non richiesto: il cliente manda solo link.
- Append di altri file a una campagna import già esistente — solo alla creazione per ora.
- Textarea paste — scelto solo upload file.
