# Contratto M2 ↔ M3 — canale WhatsApp

> Stato: **v1.0 — vincolante** · Data: 2026-07-29 · Owner: Tommaso
> Vale per: `feat/whatsapp-m2-ingest-campagne` e `feat/whatsapp-m3-invio`.
> **Ordine deciso il 29/07: prima M2, poi M3 — un cantiere alla volta.** Il vincolo è la macchina: 7,4 GB, la build frontend di M2 ne prende 2,7 e un browser di M3 1,2, più i subagent di entrambi. Il contratto resta **più** necessario, non meno: M3 partirà da una sessione che non ha visto nulla di questo lavoro.
> Base di codice: `main` @ `8b4f819` (M1 mergiato con PR #21 @ `18a6231`; PR #22 ha modularizzato i CLAUDE.md).
> ✅ Prerequisito soddisfatto: **PR #23 (CI verde) mergiata il 29/07**, `main` @ `b0bc2ac` — vedi §8.3.
> Fonti misurate: [`poc-report.md`](poc-report.md), [`wa-dom-catalog.md`](wa-dom-catalog.md), [`SDD-whatsapp-channel.md`](SDD-whatsapp-channel.md).

---

## 0. Perché questo documento esiste, e come si usa

M1 ha congelato lo schema delle otto tabelle `wa_*` prima che qualcuno scrivesse ingest o invio. È questo che rende possibile il parallelo: **M2 produce righe, M3 le consuma, e lo schema fra loro non si muove più**.

Ma disaccoppiati non vuol dire indipendenti. Ci sono **sette punti di contatto**. In sequenza si scoprirebbero strada facendo; in parallelo no — due cantieri li risolvono in modo diverso e te ne accorgi al merge, quando entrambi hanno già costruito sopra.

**Regole d'uso, non negoziabili:**

1. Il contratto **vince sui piani**. Se un piano dice una cosa e questo documento un'altra, vale questo documento.
2. Se i due cantieri si contraddicono, la contraddizione **si risolve qui** (emendamento in §9), non nei piani. I piani citano, non ridefiniscono.
3. Chi ha bisogno di violare una regola di proprietà (§4, §5) **lo dichiara prima**, non lo scopre al merge.
4. Ogni emendamento è datato e firmato in §9. Un contratto modificato in silenzio non è un contratto.

---

## 1. I sette punti di contatto

| # | Punto | Deciso a | Dove |
|---|---|---|---|
| 1 | Numerazione migrazioni | 026 → M2, 027 → M3 | §6 |
| 2 | `optout_enabled` condizionale | **M2**, alla creazione campagna | §2.1 |
| 3 | Guardia V2: `ok` non è `signal` | **M3**, con la mappa segnale→esito | §3 |
| 4 | Riattivazione numeri `retired`/`suspended` | **M2**, nel CRUD numeri | §2.2 |
| 5 | Mascheramento del numero nei log dell'ingest | **M2** | §2.3 |
| 6 | **Contratto di consegna** (stati esatti + seed) | definito qui | §7 |
| 7 | **Renderer WA dei template** (scoperto il 29/07) | **M2** in PR-0, consumato da M3 | §2.4 |

I punti 1-5 erano già decisi alla chiusura di M1: qui sono scritti in modo che un implementatore **non debba interpretare**. Il 6 e il 7 si decidono in questo documento.

---

## 2. I punti già decisi, scritti per non essere interpretati

### 2.1 `optout_enabled` — lo assegna M2, alla creazione della campagna

`wa_campaigns.optout_enabled` ha `server_default=true` a DB (migrazione 025). Quel default è **la rete di sicurezza, non la regola**. La regola della SDD (§5.2, V10) è condizionale: *"default: True se marketing"*. Una condizione non la esprime un `server_default`.

**Vincolante per M2:**

- Alla creazione di una campagna, M2 assegna **esplicitamente** `optout_enabled = (campaign_type == marketing)`.
- L'admin può poi sovrascriverlo a mano (togglabile, V10) — ma il valore iniziale è calcolato, mai lasciato al default.
- Se `optout_enabled` è `True`, `optout_cta` **deve** essere non vuota: campagna con opt-out attivo e CTA vuota = errore di validazione a 422, non una campagna che parte senza via d'uscita.
- Test obbligatorio: creare una campagna `followup` e verificare che `optout_enabled` sia `False` **nella riga a DB**, non solo nella risposta API.

**Vincolante per M3:** M3 **non scrive mai** `optout_enabled`. Lo legge per decidere se appendere `optout_cta` al testo dello **step 0** (solo il primo messaggio della sequenza, SDD §7.2), e basta.

### 2.2 Riattivazione numeri `retired` / `suspended` — è M2

M1 ha chiuso di proposito la resurrezione automatica: `_persist_status` non fa più uscire un numero da `retired`/`suspended`, perché quegli stati li mette un operatore o la piattaforma e **non sono deducibili da una lettura del DOM**. Conseguenza da guardare in faccia: **oggi non esiste alcun modo di rimettere operativo un numero ritirato.**

**Vincolante per M2** — endpoint dedicato nel CRUD numeri, con tre regole:

1. La transizione ammessa è `retired|suspended` → **`pending_qr`**. Mai → `active`. Un numero riattivato deve ripassare dalla verifica sessione (`wa_session.check_session`, M1), che lo promuove ad `active` o a `qr_required` guardando il browser vero. M2 non ha il diritto di dichiarare viva una sessione che non ha visto.
2. La riattivazione richiede un **motivo scritto** (campo obbligatorio) che finisce in `wa_numbers.notes` in append, con data. Uno stato che un umano ha messo a mano si toglie a mano, lasciando traccia.
3. Riattivare **azzera** `sent_today` e `sent_date`, e riporta `warmup_day = 1`. Un numero fermo da settimane riparte dalla rampa, non dal cap a cui era arrivato.

**Vincolante per M3:** M3 può portare un numero **in** `suspended` (segnale di ban/limitazione, FM8) e in `cooldown`, mai **fuori** da `suspended`/`retired`.

### 2.3 Mascheramento nei log dell'ingest — è M2

`PhoneNormalizationError` include il numero in chiaro nel proprio messaggio (`phone_pseudonym.py`, righe 56-74). Per un'eccezione è corretto: chi la cattura deve poter capire cosa è arrivato. Ma **il primo chiamante che facesse `logger.error(str(exc))` scriverebbe il numero nei log**, aggirando `mask_phone` e violando P12. L'ingest è quel chiamante.

**Vincolante per M2:**

- L'ingest **non logga mai** `str(exc)` di una `PhoneNormalizationError`, né la riga CSV grezza. Logga: numero di riga del file, tipo di scarto, e — se serve il valore — la sola forma mascherata.
- Il **report di scarto restituito all'admin** usa il numero mascherato. Un numero malformato spesso non è normalizzabile e quindi non è mascherabile con `mask_phone`: in quel caso nel report va la forma troncata `primi 3 + ••• + ultimi 2` costruita dall'ingest, mai il valore intero.
- Test obbligatorio in Fase 4: run E2E dell'ingest con un CSV di numeri veri, poi **grep sul file di log**: zero occorrenze di un numero completo (SDD Q87).

### 2.4 Renderer dei template WA — lo scrive M2 in PR-0, lo consuma M3

**Trovato leggendo il codice il 29/07, non era nell'elenco dei punti di contatto.** La SDD (§5.2, §6.1) dice che i template degli step si rendono "via `template_renderer.pick_template()` / `render_template()`". **Non funziona:**

- `pick_template(campaign)` legge `campaign.base_message_template`, `message_template_b/c/d` — i nomi **IG**. `WaSequenceStep` ha `template_a/b/c/d`. Passargli uno step non solleva: prende `getattr` a vuoto e ritorna il template A vuoto.
- `render_template()` conosce **solo** `{nome}`. Su `{ultimo_ordine}` — cioè esattamente i placeholder che vengono da `wa_contacts.attributes`, la ragione per cui l'ingest raccoglie le colonne libere — `RESIDUAL_PLACEHOLDER_RE` scatta e la funzione **solleva `TemplateRenderError`**.

Serve quindi `backend/app/services/wa_template.py`. Lo scrive **M2** (il set dei placeholder ammessi nasce dalle colonne del CSV, che è dominio suo) e lo consuma M3 senza modificarlo. Sta in **PR-0** (§5) proprio perché serve a entrambi il giorno 1.

**Interfaccia congelata** (M3 può scrivere il proprio codice contro questa firma prima che il corpo esista):

```python
def pick_wa_template(step, rng=None) -> tuple[str, str]:
    """Sceglie fra template_a..d compilati dello step. Ritorna (testo, variante 'a'|'b'|'c'|'d').
    Riusa la stessa logica a pesi uguali di template_renderer.pick_template, sui campi WA."""

def render_wa_template(template: str, *, display_name: str | None,
                       attributes: dict | None, rng=None) -> str:
    """spintax -> {nome} -> placeholder da attributes -> normalizzazione.
    Solleva TemplateRenderError se resta un placeholder sconosciuto o se il
    risultato e' vuoto: meglio non mandare UN messaggio che mandarne uno col
    placeholder letterale dentro."""

def validate_wa_template(template: str, *, known_attributes: set[str]) -> list[str]:
    """Ritorna la lista dei placeholder NON risolvibili con le colonne note.
    Lista vuota = template valido. Usata da M2 al salvataggio dello step."""
```

Regole di comportamento vincolanti:

- Lo spintax (`{a|b|c}`) si **riusa** da `template_renderer.resolve_spintax`, non si riscrive: una seconda implementazione dello stesso parser è una seconda occasione di divergere.
- `{nome}` cade su `display_name`; se è vuoto, il messaggio si rende **senza** il nome, non con un segnaposto tipo `@username` (che è semantica IG e su WhatsApp non esiste).
- Placeholder mancante **nel singolo contatto** (colonna presente nel CSV ma vuota per quella riga): il render **solleva**. Quel contatto va `failed` e non parte. Un messaggio che dice "il tuo ultimo ordine è " è peggio di un messaggio non inviato.
- `validate_wa_template` è il gate di M2 al salvataggio: uno step con placeholder ignoti **non si salva** (422 con la lista). Vuol dire che M3, a tempo di invio, trova solo template già validati — ma **continua comunque a gestire `TemplateRenderError`** come fallimento del singolo contatto: la validazione a monte non è una garanzia di runtime, i dati cambiano.
- `render_wa_template` **non tocca** `optout_cta`: l'append della CTA è di M3 (§2.1), dopo il render.

---

## 3. Guardia V2 — `ok` non significa "c'è la cronologia". È M3.

`OpenResult.ok = True` significa **solo** che il composer è comparso. La presenza o assenza di cronologia sta **per intero** in `signal` (`whatsapp_page.py`, `open_chat`, righe 320-365). Il POM non decide: espone segnali, la politica è di M3.

**La regola, in una riga: non si invia se `signal` non conferma la cronologia.** Se M3 la sbaglia, la guardia salta con `ok=True` e si scrive a chi aveva detto STOP.

### 3.1 Condizione di invio

Si può inviare **solo se tutte** e tre:

```
res.ok is True
res.signal.startswith("cronologia:")
int(res.signal.rsplit(":", 1)[1]) >= 1        # il conteggio bolle agganciate
```

Se il parse del conteggio fallisce (formato inatteso) → **non si invia**. Il contatto resta `queued`. Un segnale che non si sa leggere è un segnale che dice no.

### 3.2 Mappa segnale → esito del contatto

La distinzione che conta: **colpa del contatto** (→ terminale, il contatto esce dalla lista) contro **colpa nostra o dell'infrastruttura** (→ il contatto **resta `queued`**, si ferma il numero). Un selettore rotto non deve bruciare una lista (SDD §11).

| `signal` | Causa | Esito contatto | Esito numero/campagna |
|---|---|---|---|
| `cronologia:<sel>:<n>` con n ≥ 1 | ok | procede all'invio | — |
| `nessuna-cronologia:nessun-messaggio-nel-pannello` | chat aperta ma vuota (V2) | `skipped`, motivo `no_existing_chat` | — |
| `nessuna-cronologia:sezione-chat-vuota:nessuna-conversazione-esistente` | contatto esiste, nessuna chat 1:1 | `skipped`, motivo `no_existing_chat` | — |
| `nessuna-cronologia:nessuna-sezione-chat:solo-gruppi-o-contatti-senza-conversazione` | solo gruppi (fuori perimetro) | `skipped`, motivo `no_existing_chat` | — |
| `nessuna-cronologia:nessun-risultato-di-ricerca` | **ambiguo**: numero non su WhatsApp *oppure* ricerca rotta | vedi §3.3 | — |
| `nessuna-cronologia:casella-ricerca-non-trovata` | **nostra** (DOM/selettori) | resta `queued` | contatore FM2 +1 |
| `nessuna-cronologia:ricerca-non-svuotata` | **nostra** (stato pagina sporco) | resta `queued` | contatore FM2 +1 |
| `nessuna-cronologia:focus-non-sulla-ricerca-pre-invio` | **nostra** (focus perso) | resta `queued` | contatore FM2 +1 |

Il contatore FM2 è per-numero e per-sessione: **3 fallimenti consecutivi "nostri" su chat diverse** ⇒ stop invii del numero, campagna → `error`, screenshot diagnostico, alert Telegram. Nessun contatto marcato `failed` in quel giro.

### 3.3 `nessun-risultato-di-ricerca`: come si scioglie l'ambiguità

Non si scioglie in un colpo solo, si scioglie **statisticamente dentro la sessione**:

- Primo incontro: `failure_count += 1`, `last_error` scritto, il contatto **resta `queued`** con `next_action_at = now + 6h`.
- Si dichiara `skipped` + `wa_contacts.do_not_contact = True, dnc_reason = 'invalid_number'` **solo** quando: `failure_count >= 2`, i due tentativi sono avvenuti in **sessioni diverse**, e in **ciascuna** di quelle sessioni almeno un altro contatto è stato aperto con successo.

La terza condizione è il discriminatore: se nella stessa sessione altre chat si aprono, la ricerca funziona e il problema è quel numero. Se non si apre nulla, il problema siamo noi — ed è il caso in cui FM2 deve scattare, non il DNC.

### 3.4 `sync_state()` torna sempre `unknown`: cosa fa M3

Il selettore dell'indicatore di sincronizzazione non è catalogato (catturarlo richiede un re-scan del QR, che azzererebbe PoC-1 fino al 10/08), quindi `sync_state()` torna **sempre** `unknown` e non tornerà mai `synced` finché `SYNC_INDICATOR` resta vuoto.

Trattare `unknown` come blocco fail-closed letterale bloccherebbe il 100% degli invii. Trattarlo come `synced` è A9/FM16: **su una chat non ancora sincronizzata la guardia non legge un silenzio, legge il vuoto**. La politica è di M3 e il contratto fissa il perimetro:

1. **`unknown` non vale mai `synced`.** Nessun ramo di codice può scriverlo o assumerlo.
2. **Quarantena post-riconnessione.** Nei primi `WA_RESYNC_QUARANTINE_MIN` minuti dopo l'avvio o il riavvio di un browser il numero **non invia**. Valore iniziale **15 min** — ⚠️ **stimato, non misurato**: la costante porta scritta accanto la sua provenienza e va rimisurata quando il selettore verrà catturato. La sincronizzazione riparte da capo a ogni riconnessione (poc-report §Rischi 1), ed è quella la finestra cieca.
3. **Incoerenza DB↔DOM ⇒ non si invia.** Se per quel contatto esiste già almeno un `wa_messages.status = 'sent'` (qualunque campagna dello stesso tenant e numero) ma la cronologia agganciata mostra **zero** messaggi, il DOM sta mentendo: la chat non è sincronizzata. Contatto **resta `queued`**, niente invio, evento diagnostico. È il controllo compensativo più forte disponibile finché il selettore manca, e costa una query.
4. Quando il selettore verrà catalogato, la quarantena §3.4.2 **si sostituisce** con la guardia vera; il punto 3 resta comunque.

### 3.5 Ri-lettura della coda subito prima di premere invio (TOCTOU)

Fra guardia e invio passano ~20 s misurati (`guardia_totale_ms` mediana 22,2 s, poc-report). Uno STOP che arriva **dentro** quella finestra non viene visto.

**Vincolante per M3:** subito prima di `send_text`, seconda `read_inbound_tail()` — **senza** rifare `load_history()`, che è già stata fatta e costa. Se la seconda lettura torna `None` (cecità) o contiene uno STOP: **non si invia**. Le due funzioni sono separate nel POM esattamente per rendere possibile questa rilettura a costo basso (docstring di `load_history`, righe 367-384).

---

### 3.6 C4 / FM9 — "non intromettersi se sta scrivendo l'umano" non si può fare oggi

La SDD (§9, regola C4; FM9) prevede che il bot non si intrometta quando **l'ultimo messaggio in chat è dell'umano-business**: il cliente sta conversando a mano e lo step va rinviato.

**Con il POM di M1 non è implementabile**, ed è meglio saperlo adesso che scoprirlo a metà cantiere. `read_inbound_tail` **filtra via l'outbound per contratto** — è la sua garanzia contro i falsi "nessuno STOP" — quindi non esiste oggi un modo di sapere se l'ultimo messaggio della conversazione è nostro, dell'umano del cliente, o del contatto. `scan_chat_list` espone `last_is_outbound` per la **lista**, ma dice solo "l'ultimo è nostro", non distingue il bot dall'umano (quella distinzione richiede il confronto con `wa_messages.sent_at`, e la lista non porta i timestamp).

Conseguenze, esplicite:

- **M3 non implementa C4/FM9.** Non è una dimenticanza del piano: è un limite dichiarato.
- Il rischio residuo è che il bot mandi un messaggio di campagna mentre il cliente sta conversando a mano con quella persona. In MVP è **accettato**: le campagne sono a un messaggio solo, i contatti sono caldi, e la guardia pre-invio blocca comunque se il **contatto** ha risposto.
- **Va deciso prima di M4**: aggiungere al POM un metodo che legga l'ultimo messaggio a prescindere dalla direzione è un emendamento a `whatsapp_page.py` (patrimonio M1), con il suo test di non-regressione.

## 4. Proprietà delle scritture: chi scrive cosa

La regola generale: **una colonna ha un solo proprietario in scrittura.** Chi non è proprietario può leggere quanto vuole.

### 4.1 Per tabella

| Tabella / colonna | Scrive M2 | Scrive M3 | Note |
|---|---|---|---|
| `tenants` (tutto) | ✅ | — | |
| `wa_numbers`: `label`, `proxy_url`, `daily_cap`, `notes`, `browser_profile` | ✅ | — | CRUD admin |
| `wa_numbers.status` | ✅ solo `retired`/`suspended` (dismissione manuale) e riattivazione → `pending_qr` (§2.2) | ✅ `active`↔`cooldown`, → `suspended` (ban), → `qr_required` via `wa_session` | M1 possiede già `pending_qr`↔`active`↔`qr_required` dentro `wa_session._persist_status` |
| `wa_numbers`: `sent_today`, `sent_date`, `warmup_day` | ✅ **solo azzeramento** in riattivazione (§2.2) | ✅ runtime (incremento, rollover date-aware) | |
| `wa_numbers.session_checked_at` | — | ✅ (via `wa_session`, M1) | |
| `wa_contacts` (anagrafica, `attributes`, `display_name`) | ✅ | — | |
| `wa_contacts.chat_title` | — | ✅ appreso al primo invio, **solo se `title_is_number` è False** | numero in chiaro nel title = PII, resta NULL (P12) |
| `wa_contacts`: `opted_out`, `opted_out_at`, `do_not_contact`, `dnc_reason` | ✅ solo `manual` (riattivazione admin di un falso positivo) | ✅ `optout` (guardia pre-invio), `invalid_number`, `unreachable` | M4 aggiungerà la via del watcher |
| `wa_contacts.last_contacted_at` | — | ✅ | |
| `wa_contacts.last_replied_at` | — | — | è di M4 |
| `wa_campaigns`: config (`name`, `campaign_type`, `daily_limit`, `optout_*`, `active_hours_*`, `session_*`, `break_*`) | ✅ | — | M3 **legge** e obbedisce |
| `wa_campaigns.status` | ✅ `draft`→`running`, `running`↔`paused`, →`stopped` | ✅ `running`→`completed`, `running`→`error` | M2 non scrive mai `completed`: lo vede solo chi svuota la coda |
| `wa_campaigns.total_contacts` | ✅ | — | |
| `wa_campaigns`: `sent`, `failed` | — | ✅ | |
| `wa_campaigns`: `replied`, `opted_out` | — | ✅ solo `opted_out` da guardia | `replied` è di M4 |
| `wa_campaigns`: `started_at` | ✅ | — | allo start |
| `wa_campaigns.completed_at` | — | ✅ | insieme a `completed` |
| `wa_sequence_steps` (tutto) | ✅ | — | |
| `wa_campaign_contacts`: creazione righe, `status` iniziale `queued`/`skipped` da ingest | ✅ | — | |
| `wa_campaign_contacts`: `status` runtime, `current_step`, `next_action_at` runtime, `failure_count`, `last_error` | ✅ solo il **seeding iniziale** di `next_action_at` (§7.2) | ✅ tutto il resto **tranne `status=replied`** | `replied` lo scrive il reply-watcher di M4 (SDD §7.3/§7.4), coerente con `replied_at_step` già assegnato a M4 sotto — emendamento §9 03/08 |
| `wa_campaign_contacts`: `locked_by`, `locked_at` | ❌ **mai** | ✅ | M2 li **legge** per rifiutare la rimozione di una riga sotto lock fresco |
| `wa_campaign_contacts.replied_at_step` | — | — | è di M4 |
| `wa_messages` (tutto) | — | ✅ | M2 lo legge solo per la vista KPI |
| `wa_inbound_events` | — | — | è di M4 |
| `bot_state.wa_halted` (nuovo, 027) | — | ✅ | |

### 4.2 Contatori: mai read-modify-write

`wa_campaigns.sent`, `failed`, `opted_out` si incrementano **in SQL** (`UPDATE ... SET sent = sent + 1 WHERE id = :id`), mai leggendo in Python, sommando e riscrivendo. Con due worker sullo stesso numero — che il `_job_id` dedup dovrebbe impedire, ma "dovrebbe" non è una garanzia — il read-modify-write perde conteggi in silenzio.

Lo stesso vale per `wa_numbers.sent_today`, con in più il rollover date-aware: incremento e confronto della data nella **stessa** UPDATE condizionale (pattern `scrape_lookups_date`, migrazione 018), non due statement.

---

## 5. PR-0 — l'impalcatura condivisa (decisa il 29/07)

**Problema che risolve:** M2 e M3 toccherebbero entrambi `app/main.py` (registrazione router), `app/config.py` (env var), `backend/tests/conftest.py` (fixture), e M3 avrebbe bisogno di `wa_template.py` e dello script di seed che sono di M2. Ognuno di questi è un conflitto annunciato.

**Soluzione:** una PR piccola, **scritta da M2 come primissima cosa e mergiata su `main` il giorno 1**, che tocca tutti i file condivisi **una volta sola**. Dopo di essa, nessuno dei due cantieri ha più motivo di aprire un file dell'altro.

### 5.1 Contenuto esatto di PR-0

1. **Moduli router vuoti**, creati e registrati in `app/main.py` in un colpo solo:
   - `app/api/tenants.py`, `app/api/wa_numbers.py`, `app/api/wa_campaigns.py`, `app/api/wa_contacts.py` → riempiti da **M2**
   - `app/api/wa_ops.py` (kill-switch WA, stato invii, start/stop worker) → riempito da **M3**
   - Ogni file: `router = APIRouter(prefix="/wa/...", tags=[...])` e nient'altro. Registrazione con `dependencies=_protected` come gli altri (`main.py`, righe 116-129).
2. **`app/config.py`**: tutte le variabili di **entrambi** i moduli, con i default della §5.2. Dopo PR-0 nessuno dei due cantieri riapre `config.py`. Stesso per `.env.example`, che sta alla **radice del repo** (non in `backend/`).
3. **`app/services/wa_template.py`** completo, con i suoi test (§2.4).
4. **`backend/scripts/wa_seed_campaign.py`** (§7.4).
5. **`backend/tests/conftest.py`**: DB di test per-slot + lock (§8.1).
6. **`backend/tests/factories_wa.py`**: factory condivise (tenant, numero, contatto, campagna, step) così che né M2 né M3 debbano aggiungere fixture al `conftest.py` di root.

PR-0 **non contiene** migrazioni, non tocca `app/models/`, non tocca il canale Instagram. Deve passare la CI (che a quel punto è verde, §8.3) ed essere mergiabile in una sessione.

### 5.2 Variabili d'ambiente — la lista è chiusa qui

Aggiunte in PR-0, con questi nomi e questi default. Chi ne vuole una in più: emendamento (§9).

**M2 — ingest e campagne**

| Variabile | Default | Provenienza |
|---|---|---|
| `WA_INGEST_DEFAULT_COUNTRY` | `39` | SDD Q14 (numero senza prefisso = italiano) |
| `WA_INGEST_MAX_ROWS` | `5000` | SDD Q22, soft limit dichiarato |
| `WA_INGEST_MAX_ATTRS_BYTES` | `2048` | SDD Q15 |

**M3 — invio, cap, pacing**

| Variabile | Default | Provenienza |
|---|---|---|
| `WA_SEND_ENABLED` | `false` | **master switch fail-closed**: nessun invio finché non lo si accende a mano |
| `WA_DAILY_CAP_DEFAULT` | `20` | SDD §10.3, warmup giorno 1-3 |
| `WA_WARMUP_STEPS` | `20,20,30,40,60,80,100` | SDD §10.3 — ⚠️ **proposta non misurata**, A6 si verifica solo in M5 |
| `WA_SEND_DELAY_MEDIAN_S` | `90` | SDD §10.3 |
| `WA_SEND_DELAY_SIGMA` | `0.7` | SDD §10.3 |
| `WA_SESSION_MIN_MSG` / `WA_SESSION_MAX_MSG` | `8` / `15` | SDD §10.3 |
| `WA_BREAK_MIN_MIN` / `WA_BREAK_MAX_MIN` | `20` / `40` | SDD §10.3 |
| `WA_ACTIVE_HOURS` | `09:30-19:30` | SDD §10.3, `Europe/Rome` |
| `WA_RESYNC_QUARANTINE_MIN` | `15` | ⚠️ **stimato, non misurato** (§3.4) |
| `WA_GUARD_TAIL_N` | `40` | default del POM, `read_inbound_tail` |
| `WA_GUARD_HISTORY_MIN` | `80` | default del POM, `load_history` |
| `WA_LOCK_TIMEOUT_MIN` | `20` | riuso di `LOCK_TIMEOUT_MINUTES` (`campaign_orchestrator.py:63`) |
| `WA_MAX_FAILURES_PER_CONTACT` | `3` | oltre soglia → `unreachable` (SDD §8.2) |
| `WA_STOP_WORDS` | `stop,basta,cancellami,non scrivermi,unsubscribe,rimuovimi` | SDD §7.3 |
| `WA_GLOBAL_DAILY_CAP` | `200` | SDD Q70, safety valve di macchina |

`PHONE_HMAC_KEY` esiste già da M1 (`config.settings.phone_hmac_key`): **non si duplica**.

### 5.3 Proprietà dei file dopo PR-0

| Area | Proprietario | Regola |
|---|---|---|
| `app/services/wa_ingest.py`, `app/api/tenants.py`, `wa_numbers.py`, `wa_campaigns.py`, `wa_contacts.py` | M2 | |
| `frontend/**` (tutto; le pagine WA stanno in `frontend/app/wa/`, **non** in `frontend/src/app/` — questo repo non ha `src/`) | M2 | M3 è backend puro (deciso il 29/07): kill-switch e stato via API + Telegram |
| `app/services/wa_sender.py`, `wa_number_manager.py`, `wa_optout.py`, `workers/wa_worker.py` | M3 | |
| `app/api/wa_ops.py` | M3 | |
| `app/models/bot_state.py` + migrazione 027 | M3 | aggiunge `wa_halted` |
| `app/workers/task_queue.py`, `services/work_enqueue.py` | M3 | registrazione `wa_send_task` |
| `app/services/wa_template.py`, `scripts/wa_seed_campaign.py` | M2 (in PR-0) | M3 li **usa**, non li modifica |
| `app/models/wa.py`, `app/database.py`, `app/main.py`, `app/config.py`, `tests/conftest.py` | **nessuno** | congelati dopo PR-0; toccarli richiede emendamento §9 |
| `app/browser/whatsapp_page.py`, `whatsapp_selectors.py`, `services/wa_session.py`, `utils/phone_pseudonym.py` | **nessuno** | patrimonio M1. Modifiche solo con emendamento e test di non-regressione |
| `app/browser/instagram_page.py`, `context_manager.py`, `services/campaign_orchestrator.py` | **nessuno** | Instagram è in produzione |

Se una modifica a un file congelato diventa inevitabile: **si dichiara all'altro cantiere prima di scriverla**, si emenda §9, e la modifica va in una PR a sé, mergiata prima, non nascosta dentro la PR del modulo.

---

## 6. Migrazioni e ordine di merge

### 6.1 Numeri

- **026 → M2**, **027 → M3**. Head attuale: `025` (`down_revision = "024"`).
- Se un modulo non ha bisogno di migrazione, **il suo numero resta un buco**. I buchi non fanno male, le collisioni sì. (Ad oggi M2 potrebbe non averne bisogno: lo schema 025 copre già ingest e campagne. M3 ne ha bisogno di sicuro, per `bot_state.wa_halted`.)
- **`027.down_revision`**: M3 lo scrive `"025"`. Se al momento del rebase la 026 esiste su `main`, M3 lo cambia in `"026"` e **rifà il ciclo su-giù-su**. Non è un dettaglio di forma: due branch che partono entrambi da `down_revision = 025` producono due head alembic e un merge doloroso.

### 6.2 La 025 non ha mai visto Postgres — e il ciclo di prova non si fa in produzione

La migrazione 025 è provata **solo su SQLite** (ciclo su-giù-su isolato con `alembic stamp 024`). **Chi arriva primo con la propria migrazione è il primo a toccare Postgres davvero.** Va nel piano come rischio con un task suo, non come riga di checklist.

**Deciso il 29/07 — il ciclo su-giù-su va su un Postgres usa-e-getta, mai sul DB di produzione.** L'obiezione ragionevole è "tanto le tabelle `wa_*` sono nuove e separate". È vera per la 025, e falsa per tutto il resto:

1. **La 027 non tocca una tabella nuova.** Aggiunge `wa_halted` a **`bot_state`**, che è la tabella del kill-switch di Instagram: esistente, popolata, e letta da ogni worker in produzione a ogni giro.
2. **Provare una migrazione significa su-giù-su**, cioè verificare il `downgrade` — il percorso che serve il giorno in cui devi tornare indietro. In produzione quel ciclo è un `DROP COLUMN` su una tabella viva, e un `downgrade base` scritto al posto di `downgrade -1` cancella l'intero schema. L'8/07 sono nate 110 campagne fantasma in produzione partendo da un errore molto più piccolo.
3. **Su Supabase gli `ALTER` si impiccano** sui lock `idle in transaction` (regola nota del repo). Su un DB di test si aspetta e si ripete; su prod hai il bot fermo mentre indaghi.

Applicare 025+027 alla produzione **va fatto comunque**, ma è un **deploy**: una volta sola, in avanti, con `pg_dump` fatto prima. Non è un test.

**Dove girare il ciclo:** un **progetto Supabase nuovo e vuoto** (due minuti, gratis, stesso motore e stessa versione della produzione, quindi il test vale davvero; si cancella dopo). Docker su questa macchina **non è installato** — verificato il 29/07. Se il progetto usa-e-getta non si vuole creare, l'alternativa onesta è applicare in avanti su prod con backup e **dichiarare nella PR che il percorso di rollback non è stato provato**: è una scelta legittima, ma va scritta, non sottintesa.

### 6.3 Ordine di merge: M2 prima, sempre

1. **PR-0** (M2) → `main`, giorno 1.
2. **PR M2** → `main`.
3. **PR M3**: rebase su `main` aggiornato, ripasso completo della suite e del ciclo migrazioni, **poi** apertura per la review. Non il contrario: una PR rebasata dopo la review è una PR rivista due volte.

Se M3 finisce prima, la sua PR resta aperta in review e il cantiere prosegue sul suo branch. Il motivo per non invertire è leggibilità della catena alembic: con M3 prima si otterrebbe `025 → 027 → 026`, valido per alembic e incomprensibile fra sei mesi.

---

## 7. Il contratto di consegna

È il pezzo che rende possibile il parallelo: con questo scritto, **M3 lavora su righe seminate da uno script, senza aspettare la UI di M2**, e M2 sa esattamente cosa deve produrre quando la UI comincerà a produrle davvero.

### 7.1 Stato di consegna, riga per riga

Perché il worker di M3 prenda un contatto, **tutte** queste condizioni devono essere vere.

| Oggetto | Colonna | Valore richiesto | Chi lo mette | Esempio |
|---|---|---|---|---|
| `wa_campaigns` | `status` | `running` | M2 (start) | `running` |
| | `started_at` | non NULL | M2 (start) | `2026-08-03T09:31:12Z` |
| | `wa_number_id` | numero con `status = active` | M2 (creazione) | `9f2c…` |
| | `total_contacts` | > 0 | M2 (ingest) | `240` |
| `wa_sequence_steps` | almeno una riga con `step_index = 0` | `send_condition = always` | M2 | |
| `wa_campaign_contacts` | `status` | `queued` (MVP) o `in_sequence` (post-MVP) | M2 (ingest) | `queued` |
| | `current_step` | `-1` per lo step 0 | M2 (ingest) | `-1` |
| | `next_action_at` | **non NULL** e ≤ adesso | M2 (§7.2) | `2026-08-03T09:31:12Z` |
| | `locked_by` / `locked_at` | NULL / NULL | nessuno (M2 non li tocca mai) | `NULL` |
| | `failure_count` | < `WA_MAX_FAILURES_PER_CONTACT` | M3 lo incrementa | `0` |
| `wa_contacts` | `opted_out` | `false` | M2 garantisce all'ingest, **M3 ricontrolla live** | `false` |
| | `do_not_contact` | `false` | idem | `false` |
| | `encrypted_phone` | decifrabile con la `SECRET_KEY` corrente | M2 (ingest) | `gAAAAAB…` |
| `wa_numbers` | `status` | `active` | M1 (`wa_session`) | `active` |
| | `sent_today` | < cap effettivo del giorno | M3 | `4` |
| `bot_state` | `wa_halted` | `false` | M3 | `false` |
| config | `WA_SEND_ENABLED` | `true` | operatore | `true` |

**Invarianti che nessuno dei due può rompere:**

- **I1** — M2 non scrive **mai** `locked_by` / `locked_at`. Li legge soltanto (per rifiutare la rimozione di una riga sotto lock fresco: lock non NULL e `locked_at` più recente di `WA_LOCK_TIMEOUT_MIN` ⇒ 409, non cancellazione).
- **I2** — M3 non crea **mai** righe `wa_campaign_contacts` né `wa_contacts`. Se una riga manca, manca: non la si inventa a tempo di invio.
- **I3** — `next_action_at` non è **mai** NULL su una riga in stato non terminale prodotta da M2. M3 tratta il NULL come **non eleggibile** (fail-closed) e logga un warning: una riga senza appuntamento non è una riga da inviare subito, è una riga rotta.
- **I4** — La verifica di `opted_out` / `do_not_contact` fatta da M2 all'ingest **non solleva M3** dal rifarla live al momento dell'invio. Fra ingest e invio possono passare settimane, e l'opt-out può arrivare nel mezzo.

### 7.2 `next_action_at`: quando lo scrive M2

- **All'ingest**, su ogni riga creata: `next_action_at = now()` (UTC). Non NULL fin da subito (I3).
- **Allo start della campagna**, M2 ri-stampa `next_action_at = started_at` su **tutte** le righe ancora `queued`. È la re-spalmatura di SDD Q31: una campagna ingerita e lasciata in `draft` per tre settimane non deve presentarsi al worker come tremila righe scadute da giorni.
- **Al resume da `paused`**: stessa ri-stampa, stesso motivo.
- Dopo lo start, `next_action_at` è **solo di M3**.

### 7.3 Query di eleggibilità e claim (M3)

La query che M3 deve implementare, esplicitata qui perché è l'interfaccia vera fra i due moduli:

```sql
SELECT cc.id
FROM wa_campaign_contacts cc
JOIN wa_campaigns c  ON c.id  = cc.campaign_id
JOIN wa_contacts   ct ON ct.id = cc.contact_id
JOIN wa_numbers    n  ON n.id  = c.wa_number_id
WHERE c.status  = 'running'
  AND n.status  = 'active'
  AND n.id      = :number_id
  AND cc.status IN ('queued', 'in_sequence')
  AND cc.next_action_at IS NOT NULL
  AND cc.next_action_at <= :now
  AND (cc.locked_by IS NULL OR cc.locked_at < :stale_cutoff)
  AND cc.failure_count < :max_failures
  AND ct.opted_out      = false
  AND ct.do_not_contact = false
ORDER BY cc.next_action_at
LIMIT 1
```

Poi il claim atomico, stesso pattern di `browser_bio.claim_next_pending` (`browser_bio.py:375`):

```sql
UPDATE wa_campaign_contacts
   SET locked_by = :worker_id, locked_at = :now
 WHERE id = :id
   AND (locked_by IS NULL OR locked_at < :stale_cutoff)
```

`rowcount == 0` ⇒ qualcun altro l'ha preso ⇒ si passa al prossimo, senza errore.

**Un contatto alla volta, non un batch.** Il lock si tiene per la durata di **un** invio (mediana misurata 47 s, p95 60 s) e si rilascia subito dopo. Con `stale_cutoff` a 20 minuti e delay fra messaggi di ~90 s, un claim a batch parcheggerebbe righe sotto lock per l'intera mini-sessione: una sessione morta a metà le renderebbe invisibili per venti minuti a chiunque altro. La mini-sessione resta lunga; il lock no.

### 7.4 Lo script di seed — `backend/scripts/wa_seed_campaign.py`

Scritto da **M2 in PR-0**, usato da **M3** per generarsi dati senza la UI. È la ragione tecnica per cui i due cantieri possono partire lo stesso giorno.

```
python -m scripts.wa_seed_campaign \
    --tenant-label "Tenant Test M3" \
    --number-label "Numero test M3" \
    --number-phone "+39XXXXXXXXXX" \
    --browser-profile "data/browser_profiles/wa_test_m3" \
    --contact "+39XXXXXXXXXX" --contact "+39YYYYYYYYYY" \
    --campaign-name "M3 smoke" \
    --campaign-type followup \
    --template "Ciao {nome}, {questo|quello} e' un test." \
    --daily-cap 3 \
    [--start] [--dry-run] [--force-number-active]
```

Cosa scrive, esattamente:

| Tabella | Riga prodotta |
|---|---|
| `tenants` | get-or-create per `label`, `status = active` |
| `wa_numbers` | get-or-create per `phone_hmac`; `status = pending_qr`, `daily_cap` dal flag, `warmup_day = 1`, `sent_today = 0`, `browser_profile` dal flag |
| `wa_contacts` | uno per `--contact`: `phone_hmac`, `encrypted_phone`, `display_name = None`, `opted_out = false`, `do_not_contact = false` |
| `wa_campaigns` | `status = draft` (o `running` + `started_at = now` con `--start`), `campaign_type` dal flag, `optout_enabled` calcolato come §2.1, `total_contacts` = numero di contatti |
| `wa_sequence_steps` | una riga `step_index = 0`, `send_condition = always`, `wait_days = 0`, `template_a` dal flag |
| `wa_campaign_contacts` | una per contatto: `status = queued`, `current_step = -1`, `next_action_at = now`, `locked_by = NULL`, `failure_count = 0` |

Regole dello script, tutte vincolanti:

- **Idempotente**: rilanciarlo con gli stessi argomenti non duplica nulla (get-or-create su `(tenant, phone_hmac)` e su `(tenant, campaign_name)`).
- **Rifiuta di girare** se `DATABASE_URL` non è uno SQLite locale o un URL che contiene `test`, a meno di `--i-know-what-im-doing`. L'8/07 i test hanno creato 110 campagne fantasma su Supabase **produzione**: uno script di seed è esattamente la stessa arma.
- **Stampa numeri mascherati**, mai in chiaro (§2.3), e alla fine un riepilogo con gli id creati.
- `--force-number-active` mette il numero ad `active` **senza** QR: serve ai test che non aprono browser, stampa un warning grosso, e **non va usato per una prova d'invio vera** — un numero `active` senza sessione fa fallire l'apertura chat e sporca le misure.
- `--dry-run` stampa cosa farebbe senza scrivere.

⚠️ `--browser-profile` **non deve mai** puntare a `D:\dev\wa-poc\profile`: è il profilo di PoC-1, in corsa fino al 10/08, e un secondo processo che lo apre rischia di azzerare la misura.

---

## 8. Regole operative del parallelo

### 8.1 Suite pytest: il vincolo vero, e come PR-0 lo toglie

Il `conftest.py` forza `DATABASE_URL = "sqlite+aiosqlite:///./data/test_bot.db"` — percorso **relativo alla working directory** — e fa `drop_all` + `create_all` a ogni sessione di test.

Conseguenza precisa, verificata leggendo il file: **due run pytest nella stessa working directory si cancellano lo schema a vicenda** e producono rossi che sembrano regressioni (il 28/07 è costato un'ora: tre run con 24, poi 1, poi 22 falliti, tutti fantasmi, contro 729 verdi in isolamento). Due run in **worktree diversi** usano file diversi e non collidono — ma nessuno lo aveva verificato, e la regola prudenziale "una suite alla volta" era diventata un vincolo di macchina.

**PR-0 lo chiude alla radice**: il percorso diventa parametrico (`WA_TEST_DB_SLOT`, default `default`) e la sessione di test prende un **lock esclusivo sul file**, fallendo subito con un messaggio esplicito invece di produrre rossi misteriosi.

Fino a PR-0 mergiata, e comunque **dentro lo stesso worktree**, resta la regola: **una suite alla volta**.

### 8.2 RAM: il vincolo è la macchina, non il cantiere

7,4 GB totali, ~360 MB liberi con Chrome aperto. M2 ha un frontend (build Node già abbattute da ram-guard a 2,7 GB); M3 apre browser da **1,2 GB per profilo** (misurato in M0).

**Regola: un solo comando pesante alla volta a livello di macchina** — non per cantiere. "Pesante" = build frontend, `next build`, suite completa, sessione browser. Prima di lanciarne uno: `D:\dev\tools\ram-guard\guard.ps1 stato` dice quanta RAM resta e chi è stato abbattuto.

### 8.3 CI: si ripara prima, non durante

La CI su `main` è rossa **da prima di M1** (verificato su `acda5d6`, `a9a3aa8`, `52a2a92`), per due cause che non c'entrano col canale WhatsApp:

- **backend**: `ModuleNotFoundError: No module named 'app'` — il workflow muore caricando `conftest.py`, in 27 secondi, senza eseguire un solo test. Working directory / `PYTHONPATH` del workflow.
- **frontend**: 2 errori eslint (`Calling setState synchronously within an effect`) su pagine Instagram, più 10 warning.

**FATTO il 29/07 — PR #23 mergiata, `main` @ `b0bc2ac`, backend e frontend verdi.** Le cause erano **tre**, in fila, ognuna nascosta dalla precedente: `pytest` console script che non mette la CWD in `sys.path` (`pythonpath = ["."]`), il runner senza `.env` con `Settings` che fallisce all'import (chiavi usa-e-getta nel workflow + `PHONE_HMAC_KEY`), e `backend/data/` gitignorata che SQLite non crea da sola (700+ ERROR "unable to open database file"). Tutte **riprodotte in locale**, non dedotte dal log.

⚠️ **Resta rosso il check Vercel, e non è codice**: `Project framework is set to "services", but no services are declared` — configurazione del progetto Vercel, fuori dal repo, rossa identica da mesi. Va scollegata o riconfigurata da Tommaso: un check sempre rosso insegna a ignorare i check rossi.

### 8.4 Worktree, branch, commit

- Un worktree e un branch per modulo, entrambi da `main` aggiornato: `feat/whatsapp-m2-ingest-campagne`, `feat/whatsapp-m3-invio`.
- **Mai** push diretto su `main`. **Mai** un cantiere che committa nel worktree dell'altro.
- Il canale Instagram è in produzione: ogni modifica a codice condiviso porta il suo test di non-regressione **prima** della modifica.

### 8.5 Vincoli ereditati da M1 che valgono per entrambi

- **Skill `sviluppo-modulo` obbligatoria** all'avvio di ogni implementazione, con Fase 4 completa: ≥20 test funzionali e ≥30 adversarial a criterio di PASS invertito, fix loop al 100%, whole-branch review. Il tempo va **previsto nel piano**, non trattato come un dopo.
- **Nessun `xfail`**: un difetto documentato con xfail è un difetto aperto travestito da verde.
- **Ogni timeout in una costante con la provenienza scritta accanto.** In M1 un `8000` al posto del `90000` misurato ha riportato una regressione già pagata; il 28/07 un `20000` scritto a mano ha prodotto un falso "SESSIONE PERSA" su PoC-1 perché la lista chat aveva agganciato a 19.820 ms.
- **Mai un monkeypatch che si autoriferisce.** `setattr(m, "f", lambda: m.f(...))` è ricorsione infinita: 22 MB → 1350 MB in 5 secondi, misurati. Catturare il riferimento originale prima del patch.
- **Commenti e docstring in ASCII** (`gia'`, `e'`); i markdown usano gli accenti. Caratteri non ASCII nei sorgenti sempre come escape (`"\u202a"`).
- **Numeri in chiaro solo ai confini**: chiave interna `phone_hmac`, nei log la forma mascherata.
- **Non aprire il browser sul profilo di M0** (`D:\dev\wa-poc\profile`): un re-scan del QR azzera PoC-1, in corsa fino al 10/08.
- **`PLAYWRIGHT_BROWSERS_PATH` è una trappola**: il profilo di M0 è nato con chromium-1208 nella posizione di default su `C:`; su `D:\dev\.playwright-browsers` ci sono le build 149 e 151, e puntarci significa upgrade irreversibile del profilo più fingerprint diverso davanti a WhatsApp. Il comando negli handoff M0/M1 contiene ancora quella variabile: **correggerlo dove lo si trova**.

---

## 9. Emendamenti

Ogni modifica a questo contratto va scritta qui, con data, motivo e chi l'ha chiesta. Un contratto emendato in silenzio non vincola nessuno.

| Data | Cosa cambia | Perché | Chiesto da |
|---|---|---|---|
| 2026-07-29 | v1.0 — versione iniziale | — | — |
| 2026-07-29 | Base di codice: `main` è a `8b4f819`, non `18a6231` | PR #22 è entrata dopo la stesura | lead, scrivendo i piani |
| 2026-07-29 | Le pagine WA stanno in `frontend/app/wa/`, non `frontend/src/app/wa/` | Questo repo non ha `src/`: il path del contratto v1.0 era sbagliato | lead |
| 2026-07-29 | `.env.example` è alla radice del repo, non in `backend/` | Verificato sul filesystem | lead |
| 2026-07-29 | Aggiunta §3.6: **C4/FM9 non è implementabile con il POM attuale** | `read_inbound_tail` filtra via l'outbound per contratto; leggere l'ultimo messaggio a prescindere dalla direzione richiede un metodo nuovo su `whatsapp_page.py`, che è patrimonio M1 | lead, scrivendo il piano M3 |
| 2026-07-29 | **I due cantieri si fanno in sequenza, M2 poi M3**, non in parallelo | RAM: 7,4 GB totali, build frontend 2,7 GB + browser 1,2 GB + i subagent di entrambi. Il contratto resta necessario: M3 partirà da una sessione senza memoria di questo lavoro | Tommaso |
| 2026-07-29 | §6.2: **il ciclo migrazioni su-giù-su non si fa sul DB di produzione**, ma su un progetto Supabase usa-e-getta; docker non è installato su questa macchina | La 027 tocca `bot_state`, tabella viva del kill-switch Instagram; il valore del test è il `downgrade`, che in prod è un DROP COLUMN su tabella in uso | Tommaso (ha sollevato l'obiezione), lead (ha portato i tre motivi) |
| 2026-07-29 | CI verde mergiata su `main` (**PR #23**, `main` @ `b0bc2ac`): PR-0 può partire | Prerequisito §8.3 soddisfatto | Tommaso |
| 2026-08-03 | §4.1, riga `wa_campaign_contacts: status runtime...`: M3 scrive tutto il resto **tranne `status=replied`**, che scrive il reply-watcher di M4 | La riga diceva "M3 tutto il resto" senza escludere `replied`, scritta il 29/07 prima che M4 esistesse come cantiere. La SDD (§7.3/§7.4) assegna la transizione a `replied` al reply-watcher; `replied_at_step` (riga sotto) era già assegnato a M4. Nessun altro punto di attrito trovato in §4.1: `opted_out`/`dnc_reason`, `last_replied_at`, `wa_inbound_events` erano già esplicitamente di M4 | lead, pianificando M4 |
| 2026-08-03 | M4 branca da `main` **dopo** il merge di PR #28 (M3), non in parallelo | Il design M4 (lucchetto Redis per profilo, vedi piano esecutivo M4) tocca file di proprietà M3 (`wa_worker.py`, `cron_worker.py`) — stesso motivo per cui M2↔M3 non sono andati in parallelo (§6.3) | Tommaso |

---

## 10. Riferimenti

- [`SDD-whatsapp-channel.md`](SDD-whatsapp-channel.md) — §5 modello dati, §7.1-7.5 flussi, §8 state machine, §10.3 parametri anti-ban, §11 failure mode
- [`poc-report.md`](poc-report.md) — le misure: 47,2 s per invio, guardia 5,7 s, 1,2 GB per profilo, virtualizzazione lista e conversazione
- [`wa-dom-catalog.md`](wa-dom-catalog.md) — selettori con provenienza misurata
- `backend/app/models/wa.py` — le otto tabelle, congelate
- `backend/app/browser/whatsapp_page.py` — il POM: `OpenResult`, `HistoryInfo`, `ChatRow`, `classify_direction`
- `backend/app/services/browser_bio.py` — il calco dichiarato della mini-sessione per-account (claim, defer, escalation)
- `docs/superpowers/plans/2026-07-29-whatsapp-m2-ingest-campagne.md` e `…-m3-invio.md` — i due piani esecutivi che citano questo contratto
