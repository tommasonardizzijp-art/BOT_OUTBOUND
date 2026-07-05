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
- **Ora**: solo l'account che ha colpito il break.
- **Vuoi**: ogni account deve avere la sua sessione ~5-10 min di scroll + browser scraping, per abbassare il sospetto e allungare la vita di tutti i profili.
- **DECISIONE APERTA (da decidere insieme)**:
  - (a) **tutti-per-pausa**: a ogni pausa, ogni account fa la sua sessione (sfalsati, non simultanei), oppure
  - (b) **rotazione**: pausa 1 → account 1, pausa 2 → account 2, … (round-robin sulle pause).
  - In entrambi i casi: **mai simultaneo** (sfalsare) per non creare una firma sincronizzata.
- **DECISIONE APERTA**: avvio **non necessariamente all'inizio** della pausa — offset random dentro la finestra 30-45 min (ora parte subito al break). Richiede un piccolo restructure del defer (defer breve → wake → browser → defer resto).

### 4. [VALUTARE] Comportamento app-like: fetch dei post del profilo
- Aprendo un profilo, l'app vera carica anche la **griglia post** del profilo (`feed/user` / `full_detail_info`), non solo `user_info`. La nostra bio scrape fa solo `user_info` = apertura profilo "monca".
- Valutare se aggiungere un fetch dei post (o passare a `full_detail_info` che impacchetta info+post+highlights come fa l'app all'apertura profilo) per mimare meglio l'apertura reale. Vale sia per il canale API sia per il browser.

### 5. [RICERCA] Versione app IG emulata (2.3 → 2.18?)
- Verificare la **app version** emulata (nel device settings instagrapi / user-agent della sessione). Tommaso indica che siamo su "2.3" e ipotizza una "2.18" più recente.
- Valutare: (a) se la versione è vecchia/incoerente con l'app reale = firma; (b) se una versione più nuova include endpoint/comportamenti che rendono il traffico più app-like e meno detectabile. Verificare la versione instagrapi e la app_version che imposta.

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
