# DATABASE — schema BOT OUTBOUND

Tabelle, colonne significative, enum di stato e vincoli. Estratto da `CLAUDE.md` il 2026-07-29 (contenuto invariato).

> ⚠️ Il model dichiara le colonne prima che la migrazione esista sul DB: **applicare la migrazione PRIMA di far girare il codice**, altrimenti le SELECT falliscono (colonna mancante). Le migrazioni girano contro Supabase con `python -m scripts.migrate` — vedi [../setup/CONFIGURAZIONE.md](../setup/CONFIGURAZIONE.md).

---

## `instagram_accounts`
Stato degli account Instagram usati per inviare DM.
- `status` enum: `active | warming_up | cooldown | banned | challenge_required | disabled`
- `warmup_day`: 0 = non in warm-up. Incrementato ogni giorno. Controlla il limite giornaliero dinamico.
- `session_data`: JSON serializzato di instagrapi (evita re-login)
- `encrypted_password`: Fernet-encrypted, mai in chiaro
- `scrape_lookups_today`: contatore lookup `user_info_by_username_v1` eseguiti oggi; resettato dal cron `daily_reset`. Usato per il cap anti-ban (vedi `SCRAPE_DAILY_LIMIT`).

## `campaigns`
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
- `message_template_c`: template C opzionale, terza variante per il rendering locale A/B/C (Template mode, migrazione 023) — `pick_template()` sceglie a pesi uguali tra i template compilati
- `message_template_d`: template D opzionale, quarta variante (migrazione 024), simmetrico a B/C
- `ai_enabled`: bool — default **False** per le nuove campagne (rendering locale, no AI); la migrazione 023 ha impostato **True** sulle campagne preesistenti per non cambiarne il comportamento. Vedi [AI_ARCHITECTURE.md](AI_ARCHITECTURE.md) per il flusso completo
- `ai_system_prompt`: override per-campagna del system prompt AI (vuoto/null = usa `AI_SYSTEM_PROMPT`/default globale); ha effetto solo se `ai_enabled=True`
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

## `campaign_accounts`
Join table campagne ↔ account Instagram.
- `daily_limit_override`: override del limite giornaliero per questo account su questa campagna
- `is_active`: flag per abilitare/disabilitare l'account su questa campagna
- `role`: capability componibili (stringa `String(16)`, default `both`). Base: `'scraping'` (solo bio), `'dm'` (solo invio), `'both'`. Capability **inbox** (listing dei DM-thread, solo `scrape_mode='dm_threads'`), combinabile: `'inbox'`, `'inbox_scraping'`, `'inbox_dm'`, `'inbox_both'`. **Una sola** capability inbox per campagna (un account legge una sola inbox DM); gli account scraping/dm sono illimitati → bio/DM si spalmano. Fonte di verità unica: `app/utils/roles.py` (`SCRAPE_ROLES`/`DM_ROLES`/`INBOX_ROLES` + `can_scrape`/`can_dm`/`is_inbox`); **mai** filtrare per tuple inline. Scraper (bio) usa `SCRAPE_ROLES`, worker DM `DM_ROLES`, il listing inbox `INBOX_ROLES` (esattamente 1).

## `followers`
Ogni riga è un follower della pagina target in una campagna specifica.
- `status` enum: `pending → bio_scraped → message_generated → pending_approval → sent | failed | skipped | replied`
- Unique constraint: `(campaign_id, ig_user_id)` — previene duplicati nella stessa campagna
- `locked_by_account_id` + `locked_at`: optimistic locking per multi-worker (auto-released dopo 20 min)
- Colonne contatto (aggiunte in migrazione 014): `phone`, `email`, `whatsapp` (stringhe nullable), `bio_links` (JSON nullable — lista link dal profilo IG), `contact_source` (JSON nullable — quale campo/metodo ha estratto ogni dato), `contact_extra` (JSON nullable — dati grezzi aggiuntivi). Popolati da `contact_extract.py` a scrape-time o a resolve-time (import).
- Colonne del motore inbox browser (aggiunte in **migrazione 031**, tutte nullable, additiva): `last_message_at` (DateTime — data ultimo messaggio del thread), `last_message_from` (String(10) — `'us'` / `'them'`, chi ha scritto per ultimo), `last_message_text` (Text — testo integrale dell'ultimo messaggio, scelta esplicita di Tommaso), `source_channel` (String(10) — `'api'` / `'browser'`, provenienza del dato). Popolate solo dal motore browser, a chat aperta; le schede raccolte via API restano con questi campi vuoti.
- **Targa provvisoria (convenzione, non un vincolo di schema)**: il motore inbox browser non conosce il pk Instagram, quindi assegna a `ig_user_id` un numero **negativo** derivato deterministicamente dallo username (`SHA-256` normalizzato, 63 bit, negato — vedi `app/services/inbox_browser/targa.py`). Instagram non assegna mai pk negativi, quindi `ig_user_id < 0` identifica senza ambiguità un contatto ancora da arricchire. Sostituita dalla targa vera durante l'arricchimento (via browser, che naviga per username e riporta il pk). Una campagna `inbox_engine='browser'` richiede `enrichment_level != 'none'` e `bio_engine='browser'`, altrimenti la targa provvisoria resterebbe per sempre e aggirerebbe il dedup cross-campagna su `global_contacts`.

## `imported_profiles`
Tabella di staging per la modalità `source_type='import'` (migrazione 013). Ogni riga = un profilo IG fornito dall'utente via file, in attesa di risoluzione in `Follower`. Serve perché `Follower.ig_user_id` è NOT NULL + unique ma all'import si ha solo lo username (il `pk` arriva dopo la call IG).
- `status` enum: `pending → resolved | not_found | private | error`
- `raw_input`: riga originale del file; `username`: username normalizzato (lowercase)
- `ig_user_id`: popolato dopo la risoluzione (null finché `pending`)
- Unique constraint: `(campaign_id, username)` — dedup interno alla campagna
- Risolto dal worker `resolve_imports_task` (`app/services/import_resolver.py`): `user_info_by_username_v1` → crea `Follower(bio_scraped)`; riusa login/rotazione-429/session-break dello scraper. Profilo privato → `Follower` creato comunque. La dedup `global_contacts` NON avviene qui (solo a send-time).

## `messages`
Ogni DM (generato o inviato) è una riga separata.
- Collegato a follower + account che ha inviato + campagna
- `template_variant`: 'a' o 'b' per A/B testing (M10)
- Permette retry granulare

## `global_contacts`
Lead database + deduplicazione cross-campagna. Previene di inviare DM due volte allo stesso utente. Un profilo diventa un "lead visto" (`last_contacted_at=NULL`) nel momento dello scraping, anche se la messaggistica è disattivata — la colonna `last_contacted_at` viene popolata solo al primo invio DM riuscito.
- `ig_user_id` UNIQUE
- `username`, `full_name`, `biography`: dati profilo lead aggiornati ad ogni invio
- `contacted_by_campaign_ids`: JSON array di campaign_id (legacy, per backward compat)
- `contact_history`: JSON array ricco — ogni entry `{campaign_id, campaign_name, account_id, account_username, contacted_at}`
- Le colonne nuove (`username`, `full_name`, `biography`, `contact_history`) sono aggiunte via migrazione inline al boot (`ALTER TABLE ADD COLUMN` con try/except in `database.py`)
- Colonne contatto (aggiunte in migrazione 014): `phone`, `email`, `whatsapp`, `external_url` (stringhe nullable), `bio_links` (JSON nullable), `contact_source` (JSON nullable), `contact_extra` (JSON nullable). Merge cross-campagna con gap-fill: un campo viene aggiornato solo se era NULL e il nuovo valore è non-vuoto.
- `scrape_sources`: JSON array NOT NULL (default `[]`) — elenco delle sorgenti (campaign_id + timestamp) da cui il profilo è stato visto durante lo scraping, anche senza DM inviato.
- `first_seen_at`: timestamp del primo scraping (NULL su righe pre-014).

## `lead_target_profiles`, `lead_qualification_runs`, `lead_qualifications`
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

## `activity_logs`
Audit trail di tutte le azioni significative: login, scrape, dm_sent, dm_failed, rate_limited, challenge, cooldown_start/end, account_banned.

---

Vedi anche: [OVERVIEW.md](OVERVIEW.md) · [SCALA_E_PARALLELISMO.md](SCALA_E_PARALLELISMO.md)
