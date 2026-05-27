# FUTURE IMPROVEMENTS — BOT OUTBOUND

**Creato**: 2026-04-16 (Audit v2)
**Aggiornato**: 2026-04-16 (Fase E — M4, M6, M8 lite, M15 rev completati; +S1-S4 gap scraping anti-detection)
**Scopo**: Raccolta organizzata di tutti i miglioramenti proposti.

---

## ✅ Completati — Fase E (2026-04-17/18)

### ✅ M6. Conferma dialogo per azioni distruttive
- Stop campagna ha confirm dialog frontend.

### ✅ M4. Skeleton loaders durante caricamento
- Aggiunti in tutte le pagine principali (sessione 2026-04-18).

### ✅ M15 rev. Approvazione campionaria messaggi (per-campagna, opzionale)
- Toggle `require_approval` + `approval_sample_size` su Campaign
- Dopo pre-gen: N follower random → `pending_approval` status
- UI coda approvazione in dettaglio campagna con Approva/Rigenera
- Nuovi endpoint: `/approval-queue`, `/approve-message`, `/reject-message`

### M8 lite. Contatore notifiche DM per account
- **File**: `api/accounts.py` + `frontend/app/accounts/page.tsx`
- **Dettaglio**: `GET /accounts/{id}/dm-inbox-count` via instagrapi `direct_pending_count()`. Badge nella UI account. Polling ogni 5 min.
- **Stato**: Non ancora implementato — da fare.

---

## Da fare — Alta priorità

### M13. Webhook/notifiche
- **File**: Nuovo `services/notifications.py`
- **Impatto**: Alto — campagna completata, account bannato, errori
- **Complessità**: 4 ore
- **Dettaglio**: Webhook HTTP a URL configurabile. Eventi: campaign_completed, account_banned, worker_error, service_unreachable.
- **Nota**: Deferred — non urgente finché si è in fase di test locale.

### M12. Autenticazione API (JWT o API key)
- **File**: `main.py` + nuovo middleware
- **Impatto**: Critico per qualsiasi deploy non-localhost
- **Complessità**: 1 giorno
- **Dettaglio**: Middleware FastAPI con JWT bearer token o API key header.
- **Nota**: Deferred — non serve per test localhost.

### M8. Tracking risposte DM (polling inbox) — versione completa
- **File**: Nuovo `services/inbox_tracker.py`
- **Impatto**: Altissimo — sapere chi ha risposto è il dato più prezioso
- **Complessità**: 3-5 giorni
- **Dettaglio**: Cron job che controlla inbox via instagrapi. Aggiorna `follower.status = replied`. Dashboard mostra tasso risposta.
- **Rischio**: Accesso inbox potrebbe triggerare rate limit aggiuntivi. Da valutare dopo M8 lite.

### Miglioramenti scraping anti-detection

I gap seguenti sono stati identificati analizzando il flusso attuale di `scraper.py` (2026-04-16).

#### S1. Nessuna rotazione account su rate limit durante fetch bio
- **File**: `scraper.py` → `_store_followers_batch`
- **Impatto**: Alto — se l'account va in rate limit durante `user_info()` il job si ferma completamente
- **Dettaglio**: Aggiungere fallback a un secondo account attivo quando `user_info()` riceve 429. Richiede refactor di `_store_followers_batch` per ricevere lista di client disponibili e ruotare in caso di errore.
- **Complessità**: Media (4-6 ore)

#### ~~S2. Proxy non separato per scraping~~ — SKIPPED

- **Decisione**: Non implementato. L'utente usa hotspot mobile (4G) — l'IP è già diverso da un IP fisso residenziale. Il rischio S2 (stesso IP per scraping e DM) non si applica in questa configurazione.
- **Rivalutare se**: si passa a connessione fissa o si usano 3+ account senza hotspot.

#### ✅ Opzione A — JS override fingerprint diversificazione (implementata 2026-04-22)

- `fingerprint.py`: WebGL renderer/vendor per-account (8 profili GPU reali), screen size mascherata, timing multiplier 0.80–1.30.
- `context_manager.py`: override `window.screen`, WebGL getParameter, AudioContext, measureText font noise, Canvas toBlob.
- `dm_sender.py` + `instagram_page.py`: timing_multiplier propagato, browse time e typing scalati per account.

#### S3. Nessun delay random tra follower durante paginazione lista
- **File**: `scraper.py` → `_scrape_paginated`
- **Impatto**: Medio — il fetch della lista 50 follower avviene senza pausa interna; solo il delay tra batch (5-15s) è randomizzato
- **Dettaglio**: Aggiungere micro-delay lognormale (0.5-2s) tra la chiamata `user_followers_v1_chunk` e il `_store_followers_batch` per rendere il pattern di chiamate meno meccanico.
- **Complessità**: Bassa (30 min)

#### S4. `user_info()` per ogni follower è la chiamata più a rischio
- **File**: `scraper.py` → `_store_followers_batch`
- **Impatto**: Medio-Alto — 50 chiamate consecutive `user_info()` sullo stesso account è il pattern più riconoscibile da IG
- **Dettaglio**: Opzioni da valutare: (a) limitare `user_info()` solo ai follower con bio non vuota dal profilo short, (b) aggiungere delay lognormale più alto (3-8s) tra chiamate, (c) saltare `user_info()` ogni N follower e usare solo i dati short disponibili.
- **Complessità**: Media (2-3 ore per opzione a/b; opzione c richiederebbe refactor del modello)

---

## Da fare — Media priorità

### Cloud LLM support (estensione M14)
- **Impatto**: Alto — Ollama locale lento, qualità variabile
- **Complessità**: Media (2-4 ore)
- **Dettaglio**: Supporto OpenAI/Anthropic API come alternativa a Ollama. Config `AI_PROVIDER=ollama|openai|anthropic`.
- **Nota**: Rimandato — focus su flusso durante fase di test.

### Account Health Score avanzato (estensione M9)
- **Impatto**: Alto — monitoraggio salute account completo
- **Complessità**: Media (da valutare impatto su DB)
- **Dettaglio**: Tasso risposta DM, indice interazione, naturalezza movimento.
- **Prerequisito**: M8 completo per dati risposta. Rimandato.

---

## Da fare — Roadmap pianificata

| Feature | Stato | Riferimento |
|---|---|---|
| **7B. Multi-campagna parallela** | Pianificata | `docs/project/PROGRESS.md` |
| **8. Proxy management avanzato** | Pianificata | `docs/project/PROGRESS.md` |
| **9. Appium dispositivi Android** | Roadmap lungo termine | `docs/project/PROGRESS.md` |

---

## Completati (Fase A — 2026-04-16)

### ✅ M11. Indici DB critici (BUG-NEW-03)
### ✅ BUG-NEW-01. Deduplicazione cross-campagna (atomic INSERT OR IGNORE)
### ✅ BUG-NEW-02. Scraper risponde a pause/stop
### ✅ BUG-NEW-04. Campaign completion TOCTOU
### ✅ BUG-NEW-19. WAL pragma prima di create_all
### ✅ BUG-NEW-20. SQLite busy timeout=30s

---

## Completati (Fase B — 2026-04-16)

### ✅ BUG-NEW-05. Reset campagna cancella messaggi vecchi
### ✅ BUG-NEW-06. Retry messaggio verifica campaign status
### ✅ BUG-NEW-07. Health check DB reale (SELECT 1)
### ✅ BUG-NEW-08 / M1. Paginazione follower frontend (next/prev)
### ✅ BUG-NEW-11 / M5. SWR error handling (banner errore backend offline)
### ✅ BUG-NEW-12. Redis pre-flight check prima di avviare campagna
### ✅ BUG-NEW-15. Cleanup profili browser Chromium su delete account
### ✅ BUG-NEW-18. Message.updated_at aggiunto a modello e schema

---

## Completati (Fase C — 2026-04-16)

### ✅ M2. Filtri messaggi per campagna/account
### ✅ M3. Edit template campagna
### ✅ BUG-NEW-22. Follower fields popolati
### ✅ BUG-NEW-10. Validazione form nuova campagna

---

## Completati (Fase D — 2026-04-16)

### ✅ M7. Pagina /leads (GlobalContact browser + filtri + insights + CSV export)
### ✅ M9. Metriche account (endpoint + widget UI)
### ✅ M10. A/B testing messaggi
### ✅ M14. Pre-generazione messaggi batch

---

## Sviluppi futuri — identificati sessione 2026-04-18

### F1. Feedback loop AI — ottimizzazione prompt da reply rate

- **Impatto**: Alto — oggi i prompt vengono scritti a mano e non c'è modo di sapere quale variante converte meglio
- **Complessità**: Media (1-2 giorni)
- **Dettaglio**: Aggregare per campagna/template: messaggi inviati, risposte ricevute, reply_rate per variante A/B. Mostrare nella pagina campagna un breakdown "Template A: 12% risposta / Template B: 8% risposta". Nel tempo permette di evolvere i prompt verso quelli che generano più engagement.
- **Prerequisito**: A/B testing già implementato (M10) + reply checker già attivo

### ✅ F2. Conflitto sessione scraper/reply checker — RISOLTO

- Implementato via per-account `asyncio.Lock` in `utils/instagrapi_client.py` (sessione 2026-04-18).
- Scraper e reply checker condividono la stessa funzione `login()` serializzata: non due login concorrenti sullo stesso account.
- Alternativa con flag `use_for_reply_check` su account non implementata — il mutex è sufficiente e non richiede configurazione aggiuntiva.

### F3. Fase 7B — Multi-campagna parallela

- Già in roadmap pianificata (vedi sezione sopra)
- `current_campaign_id` su `InstagramAccount` per impedire assegnazione a più campagne
- Complessità: 1 giorno

### ✅ F4. Warm-up automatico avanzato — RISOLTO

- `advance_warmup_if_needed()` con guard `warmup_advanced_date` — idempotente, chiamata al boot E dal cron (sessione 2026-04-18).

---

## Completati (sessione 2026-04-18 — fix BUG-NEW-33/35)

### ✅ BUG-NEW-35. Conflitto sessione instagrapi scraper/reply_checker
### ✅ BUG-NEW-33. Alembic — migrazione iniziale completa, auto-upgrade al boot

---

---

## Completati (sessione 2026-04-18 — anti-detection S1/S4 + docs)

### ✅ BUG-NEW-32. _human_click verifica + typo system + selector logging
- `send_dm`: track `navigated_to_direct` flag — no più `pass` silenzioso su timeout navigazione
- Selettori input ridotti a 4 specifici DM (rimosso `div[contenteditable="true"]` — troppo generico)
- Log URL dopo navigazione + log selettore input che ha funzionato (`logger.debug`)
- Screenshot automatico `data/debug_no_input_{username}.png` quando input non trovato
- Errore diagnostico: distingue "click non ha aperto DM" da "input non trovato dopo navigazione"
- `_human_type`: typo system — ~8% prob per char in parole >3 lettere → digita tasto adiacente QWERTY → pausa 150-500ms → Backspace → ridigita corretto
- `_QWERTY_ADJACENT` dict + `_typo_char()` helper a livello modulo

### ✅ 7B Lite. Warning account già in uso su altra campagna running
- Backend `campaign_accounts.py`: check pre-assign — se account_id è in `campaign_accounts` di campagna running/paused ≠ corrente → 409 con detail `ACCOUNT_IN_USE:"campagna1"`. Query param `?force=true` bypassa il check.
- Frontend `api.ts`: `assign()` accetta `force` param → aggiunge `?force=true` alla URL.
- Frontend `campaigns/[id]/page.tsx`: `handleAddAccount(force)` — se 409 con `ACCOUNT_IN_USE:` prefix → apre `ConfirmDialog` warning con spiegazione (browser in serie non parallelo) + bottone "Assegna comunque" che ri-chiama con `force=true`.

### ✅ S1. Rotazione account su 429 durante bio fetch
- `_get_fallback_account(db, exclude_id)` — helper per account alternativo
- `_store_followers_batch` ritorna `(stored, client, account)` — client/account aggiornato al chiamante
- Se fallback disponibile: login + sleep 15-30s + retry. Se no: sleep 60s flat.

### ✅ S4. Delay lognormale 3-8s tra user_info()
- `min(8.0, max(3.0, random.lognormvariate(math.log(4.0), 0.4)))` — mediana ~4s

### ✅ SEC-05. Validazione proxy URL nel form account
- Regex `^https?://([^@]+@)?[^:]+:\d+$` applicata prima del submit
- Toast errore con formato corretto se invalido

### ✅ Guida aggiornata
- Sezione 10 "Anti-ban": espansa da 7 bullet a tabella completa con 14 protezioni attive (timing, sessioni/browser, account/dedup), tabella limiti produzione
- Sezione 12 "Azioni rischiose": nuova — tabella 8 azioni pericolose con rischio/causa/rimedio + 3 sequenze operative corrette + DangerCallout

---

*File aggiornato: 2026-04-18*
