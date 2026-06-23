# Design — Scraping contatti dai DM già avviati (`scrape_mode = dm_threads`)

Data: 2026-06-23
Stato: approvato (brainstorming), pronto per il piano di implementazione
Scope: Fase 1 — costruzione lista contatti dall'inbox + bio. La logica di messaggio/re-engage è fuori scope.

---

## 1. Obiettivo

Aggiungere una nuova modalità di scraping che, dato **un account**, raccolga i contatti dalle **conversazioni DM già esistenti** di quell'account (non dai follower/following di un target esterno), per poi qualificarli e — in una fase successiva, già coperta dal flusso AI esistente — ri-contattarli dallo stesso profilo (continuazione del thread esistente → minor rischio ban).

Caso d'uso reale: account **principali** di aziende, dove la prudenza anti-ban è prioritaria.

## 2. Decisioni prese in fase di brainstorming

| Tema | Decisione |
|---|---|
| Perimetro thread | Tutti i thread **1-a-1**, **entrambe le direzioni** (outbound + inbound). Gruppi esclusi. Pending/richieste esclusi. |
| Profondità scan | Fino all'inizio dell'inbox, **a sessioni** (riusa session-break esistente). |
| Engine di estrazione lista | **Selezionabile per campagna**: `browser` (Patchright, prudente, default) oppure `api` (instagrapi, veloce, più rischio). |
| Fase Bio | Resta su **API privata** (`user_info`) per ora. Rischio residuo documentato (vedi §8). |
| Vincolo account | **Esattamente 1 account** per campagna in modalità inbox, **hard-coded** su 3 livelli. |
| Messaggio re-engage / filtro anti-spam | **Fuori scope Fase 1.** |

## 3. Vincolo strutturale: 1 inbox = 1 account

L'inbox è privato dell'account: solo quell'account legge i propri DM. Niente rotazione pool (a differenza del follower-scraping, dove qualsiasi account scrapa un target pubblico). Conseguenze:
- Il carico cade tutto su un solo account → ragione in più per il ritmo a sessioni.
- Il vincolo "1 account" va blindato nel codice (§7).

## 4. Architettura

### 4.1 Astrazione `InboxListSource`

Interfaccia unica sopra la Fase Lista, con due implementazioni intercambiabili selezionate da `Campaign.inbox_engine`:

```
InboxListSource:
    async def resume(saved_keys: set[user_id]) -> None
        # porta il source al "fronte": salta (veloce) i contatti già salvati
        # finché smette di vederne di noti. Usato sia a start sessione sia
        # dopo un cambio engine. Correttezza garantita dal dedup, non dal cursore.
    async def next_page(state) -> InboxPage
        # InboxPage = { participants: list[(user_id, username)], resume_state, exhausted: bool }
```

**Resume-by-frontier (perno per il riavvio e lo switch engine):** il cursore/marker persistito in `scrape_cursor` è solo un'**ottimizzazione intra-engine**. Il vero meccanismo di ripresa è il **dedup**: ogni engine, riavviato, salta i `Follower` già salvati di questa campagna fino al fronte. Questo rende ogni riavvio idempotente (mai duplicati) **a prescindere** dall'engine usato prima — base dello switch a metà campagna (§4.5).

| | `ApiInboxSource` | `BrowserInboxSource` |
|---|---|---|
| Meccanismo | `client.private_request("direct_v2/inbox/", params={cursor, thread_message_limit})` | Patchright: scroll del pannello `/direct/inbox/`, estrazione handle dalle righe-thread |
| Pagina | ~20 thread/chiamata (dimensione fissa IG) | N righe per ciclo di scroll |
| Ripresa tra sessioni | **Pulita**: `inbox.oldest_cursor` persistito in `Campaign.scrape_cursor` | **Marker di profondità + dedup** (vedi §6.1): in-sessione il filo si tiene da sé; al break duro si salva il thread_id più vecchio raggiunto e al resume si fa fast-scroll fino al fronte |
| Fine | `inbox.has_older == false` | nessuna nuova riga dopo scroll (raggiunto il fondo) |
| Impronta per-richiesta | Più alta (client non-app, fingerprintabile) | Più bassa (browser reale, header/cookie/JS veri) |
| Velocità | Più veloce | Più lento |
| Target d'uso | Account secondari | **Account principali** |

Entrambe sboccano nello **stesso** codice a valle: dedup, scarto gruppi, inserimento `Follower(status=pending)`.

### 4.2 Modulo `scrape_inbox.py`

Specchio di `scrape_list.py` (Fase Lista follower). Riusa: `_list_page_delay()` (o variante `INBOX_*`), session-break via `Retry(defer=...)`, challenge handler (`isolate_challenged_account`), persistenza `Follower`, patch MediaXma al login.

Flusso:
1. Login dell'unico account proprietario.
2. Istanzia `InboxListSource` in base a `campaign.inbox_engine`.
3. Loop pagine:
   a. `page = await source.next_page(state)`
   b. Per ogni thread: estrai i partecipanti non-self. Se `len(other_users) != 1` → **scarta** (gruppo).
   c. Dedup sull'username/user_id già presenti come `Follower` di questa campagna.
   d. Inserisci `Follower(status=pending, follower_username, follower_user_id)`.
   e. Persisti `scrape_cursor` (solo engine `api`) o marker di profondità (engine `browser`, best-effort).
   f. `_inbox_page_delay()` + check session-break/halt.
4. Stop quando `page.exhausted` (inizio inbox raggiunto) → `scrape_outcome = "completed"`.

### 4.3 Estrazione partecipante (entrambi gli engine)

Logica identica a `reply_checker.py:160-175`:
```
other_users = [u for u in thread.users if int(u.pk) != own_pk]
if len(other_users) != 1:   # gruppo → skip
    continue
user = other_users[0]   # (pk, username)
```
Per il browser: ogni riga-thread espone l'handle nel link `/direct/t/<thread_id>/` + testo username; i gruppi si riconoscono da titolo multi-utente / assenza di un singolo handle risolvibile → skip.

### 4.4 Fase Bio (invariata)

`scrape_bios.py` pesca i `Follower(status=pending)`, chiama `user_info` (API) → bio + `extract_contacts`. Nessuna modifica. Vedi rischio residuo §8.

### 4.5 Switch engine a metà campagna (in scope)

Caso d'uso: parti `browser` su un account principale, scopri che l'inbox è enorme → passi ad `api` senza perdere i contatti già raccolti. Reso possibile dal **resume-by-frontier** (§4.1): i cursori dei due engine sono incompatibili (api = `oldest_cursor` opaco; browser = marker `thread_id`) ma **non vanno tradotti** — al cambio engine si invalida il cursore engine-specifico e si riparte dal fronte via dedup.

Meccanica:
1. `PATCH /campaigns/{id}` consente di cambiare `inbox_engine` **solo a campagna in pausa** (non in mezzo a una pagina in corso). Il vincolo 1-account resta identico.
2. Allo switch: si azzera `scrape_cursor` (il token vecchio non è valido per il nuovo engine) e si setta un flag/heuristica perché il nuovo engine usi `resume(saved_keys)` invece del cursore.
3. Ripresa: il nuovo engine fast-forwarda fino al fronte, poi raccoglie normalmente.

Costi per direzione (esposti in UI):
- **browser → api**: re-traversata via API = **economica** (veloce). Direzione consigliata, è la valvola di fuga per inbox enormi.
- **api → browser**: il browser deve fast-scrollare fino al fronte = **costoso** sulla profondità già raggiunta (su inbox grandi reintroduce la spirale). **Warning in UI: sconsigliato su inbox grandi.**

Fuori scope (vedi §11): lo switch **automatico** in base alla dimensione/spirale rilevata — si costruisce sopra questo switch manuale, è un future improvement.

## 5. Modello dati

- `Campaign.scrape_mode`: nuovo valore ammesso `'dm_threads'` (oggi `followers` | `following`). È `String`, nessuna migrazione per il valore.
- **`Campaign.inbox_engine`**: nuova colonna `String(10)`, default `'browser'`, nullable. → **Migrazione Alembic 019**.
- `Campaign.scrape_cursor`: **riuso** della colonna esistente per il resume intra-engine — `oldest_cursor` (api) o marker `thread_id` (browser). **Azzerata al cambio engine** (§4.5); il resume cade allora sul dedup-frontier.
- `Campaign.target_username` / `target_user_id`: valorizzati con l'**account proprietario** dell'inbox (self).
- `source_type` resta `'scrape'`.
- `Follower`: riuso schema esistente, nessuna colonna nuova.

## 6. Anti-detection

- **1 account, nessuna rotazione** (è strutturale).
- Engine `api` (account secondari): stesso pacing del follower scraper (`INBOX_API_*`, default ≈ `list_*`: delay 5-10s, pausa lunga 30-60s ogni ~15-20 pagine).
- Patch MediaXma già attiva al login instagrapi.

### 6.1 Pacing e "filo dello scroll" — engine browser

Modello: 20 min di scroll dritto non è credibile → burst di scroll alternati a distrazioni, con break duri brevi.

**Parametri `INBOX_BROWSER_*` (default proposti, tutti in `.env`):**
- Scroll step: **2-6s**, varianza lognormale.
- Micro-pausa distrazione **in-place**: ogni ~8-15 step, sleep **5-30s** (eventuale mouse-jiggle). Non naviga → posizione preservata.
- Feed-browse distrazione: con prob. ~5%, apre il feed in una **seconda tab**, scrolla **20-60s**, chiude la tab, torna all'inbox. La tab inbox resta scrollata in posizione.
- Break duro: **30-60min** via `Retry(defer=...)` (no 1-2h).

**Tenere il filo (3 livelli):**
1. **In-sessione**: micro-pause in-place e feed-browse su tab separata **non toccano** lo scroll dell'inbox → filo gratis. Mai navigare via dalla tab inbox e tornare (l'inbox si ricarica dall'alto).
2. **Correttezza al resume**: il dedup sui `Follower` già salvati rende il re-scroll **idempotente** (mai duplicati), a prescindere dal marker.
3. **Efficienza al resume (marker)**: al break duro si persiste in `Campaign.scrape_cursor` il **thread_id più vecchio raggiunto** + conteggio. Al resume: riapri inbox → **fast-scroll** (senza i delay 2-6s) attraverso la zona già vista fino a superare il marker → riprendi la raccolta a ritmo normale dal fronte. Così non si ricomincia da zero.

Nota: l'inbox è ~cronologico per ultima-attività; un messaggio in arrivo durante il break può rimescolare la cima. Il dedup lo assorbe (dup in cima scartato); il marker serve solo a saltare in fretta il già-fatto, non a garantire l'ordine.

## 7. Vincolo 1-account — hard-coded (3 livelli)

1. **Start campagna**: se `scrape_mode == 'dm_threads'` e gli account attivi assegnati ≠ 1 → errore, la campagna non parte.
2. **API assegnazione account**: tentativo di assegnare un 2° account a una campagna `dm_threads` → HTTP 400.
3. **Frontend**: in modalità inbox il selettore account accetta un solo profilo.

Vale per entrambi gli engine in fase lista.

## 8. Rischio residuo documentato — Fase Bio su API

La Fase Bio fa una `user_info` **via API privata** per profilo: su un inbox grande sono **più chiamate API della fase lista stessa**. Su un account principale questo riduce il beneficio della scelta `browser` in fase lista. Accettato per Fase 1 (consegna più piccola). **Mitigazione futura** (non in questo scope): portare anche la bio su browser (visita-profilo Patchright — il codice già visita i profili per i DM), agganciata allo stesso radio engine.

## 9. Frontend

- Form nuova campagna: opzione `scrape_mode` → "DM già avviati (inbox)".
  - Quando selezionata: nascondi il campo `target_username` (è l'account stesso); mostra radio **engine**: "🛡️ Browser (prudente, lento)" (default) / "⚡ API (veloce, più rischio)".
  - Selettore account limitato a 1 profilo.
- Dettaglio campagna: controllo per **cambiare engine a campagna in pausa** (§4.5), con warning sulla direzione `api → browser` ("sconsigliato su inbox grandi").

## 10. Test

- **Unit estrazione**: thread 1-1 outbound, 1-1 inbound, gruppo (skip), dedup su contatto già salvato. Mock dell'adapter `IGClient` (Protocol in `adapters/instagram.py`).
- **Unit cursore api**: avanzamento `oldest_cursor`, stop su `has_older == false`, ripresa da `scrape_cursor` persistito.
- **Unit/idempotenza browser**: re-scroll su lista parzialmente già salvata → nessun duplicato inserito.
- **Switch engine (§4.5)**: cambio `inbox_engine` a campagna in pausa → `scrape_cursor` azzerato, nuovo engine fa resume-by-frontier, nessun duplicato, contatti pre-switch conservati. Test su entrambe le direzioni.
- **Guard 1-account**: start con 0 e con 2 account → errore atteso; API assegnazione 2° account → 400.

## 11. Fuori scope Fase 1

- Messaggio di re-engage / lettura ultimo messaggio del thread.
- Filtro anti-spam ("salta chi è in conversazione viva / ha già risposto", basato su `Follower.status == replied` da `reply_checker`) — da introdurre nella fase messaggi.
- Pending/richieste inbox.
- Bio via browser.
- **Switch engine automatico** in base alla dimensione inbox / spirale rilevata — si costruisce sopra lo switch manuale (§4.5), future improvement.

## 12. Rischi e punti aperti

- **`_fetch_inbox_page` api**: la helper `direct_threads(amount)` non espone un cursore persistibile → serve scendere a `private_request("direct_v2/inbox/")` parsando `threads` / `oldest_cursor` / `has_older`, con `thread_message_limit` minimo (in Fase 1 servono solo i partecipanti). È il punto nuovo più delicato dell'engine api.
- **Browser virtualizzato**: la lista DM ricicla i nodi DOM durante lo scroll → estrazione **incrementale** obbligata (raccogli a ogni step, non a fine scroll) e dedup robusto.
- **Feed-browse su tab separata**: gestire correttamente apertura/chiusura della 2ª pagina Patchright senza perdere il context né la tab inbox; fallback = solo micro-pause in-place se la 2ª tab dà problemi.
- **Fast-scroll al resume** su inbox enormi: ripassare la zona già vista resta un costo (anche se senza i delay); il marker lo riduce ma non lo annulla. Accettabile per Fase 1.
