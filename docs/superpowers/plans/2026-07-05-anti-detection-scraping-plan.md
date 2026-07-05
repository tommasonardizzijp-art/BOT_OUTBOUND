# Piano anti-detection scraping — 2026-07-05

> **STATO: DA APPLICARE TUTTE INSIEME.** Il bot è in test in background. Non toccare il
> runtime finché la campagna non viene fermata. Applicare il blocco di modiche, poi
> **un solo restart** dei worker con le feature accese.
>
> Branch: `feat/warmup-browser-alternato`

---

## Contesto (la diagnosi consolidata)

Il checkpoint "sospettiamo attività automatizzate" NON è IP/proxy né volume: è il pattern
di accesso API "nudo" di instagrapi (solo read follower/user_info) + sessione web-born su
device sintetico. Vedi memory `botoutbound-checkpoint-pattern-api`. Strategia scelta:
mitigazione (comportamento più umano) + alternanza browser, NON riscrittura totale.

## Già fatto sul branch (OFF di default = zero impatto finché non si accende)

- **Warm-up scroll** nella pausa lunga bio — `warmup_browse_enabled` (OFF). Riusa `InstagramPage.browse_feed`.
- **Browser bio a BLOCCO** nella pausa — `bio_browser_batch_enabled` + `bio_browser_batch_min/max` (10-15). Una sessione: scroll poi batch di N profili. `run_pause_browser_activity` in `browser_bio.py`.
- **Mapping web→contatti** testato (`test_browser_bio_mapping`, 4 casi). ⚠️ **Estrazione live NON validata** (cattura `web_profile_info` nel browser: da provare su IG reale).
- Warm-up/batch girano solo sull'**ultimo account usato** al break (vedi #3).

---

## Modifiche da fare (blocco unico)

### 1. Cap sessione randomico 150-300 (ora fisso 250) + rivedere micro-yield
- **Cosa**: `scrape_session_size` fisso 250 → valore **random in [150, 300] per sessione**.
- **Vincolo**: il break è ancorato con formula deterministica (`next_long_break`) per sopravvivere ai restart del job (micro-yield). Un random ricalcolato a ogni restart sposterebbe l'ancora → doppio-trigger/salto. Quindi il valore random va **persistito in DB** (campo su `campaigns`, es. `current_session_cap`), fissato all'inizio di ogni mini-sessione e riletto ai restart.
- **Micro-yield (ora 100 bio)**: rivalutare. Con cap ≤300 e delay più alti (#2), la soglia 100 ha meno senso. Obiettivo: tenere il **singolo job < `job_timeout=3600s`**. Verificare la math reale (300 bio × delay medio effettivo) e alzare/semplificare la soglia di conseguenza. NON rimuovere del tutto finché non si è certi che 300 bio col nuovo delay stiano sotto 3600s.

### 2. Delay tra bio: da ~fisso a lognormale ad alta varianza [BUG]
- **Problema (segnalato Tommaso)**: la cadenza è ~fissa. Con 4 account in parallelo IG vede uno screening ogni ~40s regolare (il delay è GLOBALE per-lead → per-account ≈ delay×N_account). `random.uniform(bio_fetch_delay_min, max)` con range stretto/uguale = poca varianza.
- **Fix**: sostituire l'uniform con un **generatore lognormale a sigma alto** (come Fase Lista/DM, vedi `utils/timing.py`). Allargare il range. Verificare i valori `bio_fetch_delay_min/max` della campagna in test (sospetto min≈max≈alto → cadenza piatta).
- **Obiettivo**: cadenza irregolare, niente periodicità leggibile.

### 3. Warm-up + browser batch nella pausa su TUTTI gli account (non solo l'ultimo)
- **"Solo l'ultimo usato" (chiarimento)**: al break, il codice chiama `run_pause_browser_activity(campaign, db, account.id, ...)` dove `account` è la variabile che tiene l'ULTIMO account che ha fatto una `user_info_v1` prima che scattasse la pausa. Il loop bio ruota gli account round-robin (`ScrapingPool`), quindi `account` è semplicemente quello che ha fatto l'ultima lookup. La sessione scroll+batch gira su QUEL solo account; gli altri 3 non ricevono nulla. È questo il limite da togliere.
- **DECISIONE PRESA (05/07)**: **tutti-per-pausa** (non rotazione). A ogni pausa, OGNI account della campagna fa la sua sessione ~5-10 min di scroll + browser scraping.
- **Esecuzione**: in **parallelo** ma con **partenze scaglionate** — offset random **1-3 min tra un account e l'altro**, MAI tutti nello stesso istante (già partono insieme lo scraping API; vederli poi fermarsi e scrollare tutti nello stesso momento è una firma). Rispettare `max_concurrent_browsers` (semaphore, ora 3): con più account dei browser disponibili, si accodano.
- **Offset random dentro la pausa: NON necessario** (conferma Tommaso). Possono partire a inizio pausa; la randomizzazione che conta è lo **stagger tra account**, non l'offset nella finestra. (Niente restructure del defer.)

### 4. Comportamento app-like: `full_detail_info` invece di `user_info_v1` — DA FARE
- **Verificato (05/07)**: l'app IG reale, all'apertura di un profilo, NON fa solo `user_info` — carica anche **griglia post + highlights + relazione** in un bundle. L'endpoint è `users/{pk}/full_detail_info/` (info + prima pagina post + highlights + reel in UNA call): è ciò che spara l'app quando apri un contatto. Un bare `user_info_v1` è la firma da bot che Tommaso vuole togliere.
- **Fattibilità (verificato)**: instagrapi **2.3.0** NON ha un metodo `full_detail_info`, MA è chiamabile via `client.private_request("users/{pk}/full_detail_info/")`. **NON serve l'upgrade instagrapi** (punto 5).
- **DA FARE**: nella Fase Bio (`fetch_and_store_bio`, scraper.py) sostituire `user_info_v1` con `full_detail_info`, estrarre `user_detail.user` per bio/contatti (stesso `extract_contacts`), scartare il resto del bundle (post/highlights servono solo a rendere la chiamata app-like, non li salviamo). Validare in locale la shape della risposta e che i campi contatto ci siano. Fallback a `user_info_v1` se l'endpoint desse errore.

### 5. Upgrade instagrapi 2.3.0 → più recente — RIMANDATO / opzionale
- **Verificato (05/07)**: installata **instagrapi 2.3.0**; emula app IG **364.0.0.35.86 / 385.0.0.47.74** (il "2.18" era la versione instagrapi ipotizzata, NON l'app IG). L'upgrade bumperebbe app_version + signing/header.
- **Decisione**: **RIMANDATO**. Lungo/rischioso (conferma Tommaso). Il punto 4 dà il beneficio app-like senza questo upgrade. Fare solo se emerge che la app_version 364/385 è essa stessa flaggata.

### 6. [VALIDARE LIVE] Cattura `web_profile_info` nel browser
- Confermare su IG reale: quale endpoint serve i dati profilo oggi (REST `web_profile_info` vs GraphQL doc_id) e a quanti profili/ora scatta il 429 in-browser. Test locale controllato con `bio_browser_batch_enabled=True`, guardando i log `[BioBrowser] batch di N profili`.

### 7. [RIMANDATO] Fase Lista
- Warm-up / anti-detection sulla Fase Lista: rimandata su decisione Tommaso, dopo il resto.

---

## Ordine consigliato di applicazione
1. #2 (delay lognormale) — piccolo, alto impatto, indipendente.
2. #1 (cap random + micro-yield) — tocca la persistenza, isolato.
3. #3 (warm-up/batch tutti gli account) — dopo aver deciso (a) vs (b) e l'offset.
4. #6 (validare estrazione browser) — test locale prima di alzare i volumi.
5. #4 e #5 (valutazioni app-like + versione) — ricerca, poi eventuale implementazione.
6. #7 (Lista) — ultimo.

## Note operative
- Applicare a bot FERMO, poi un solo restart con `warmup_browse_enabled=True` + `bio_browser_batch_enabled=True`.
- Ogni modifica al pacing/anti-detection va con un test che ne blocca la regressione (vedi `tests/`).
