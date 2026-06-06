# Advanced Scraping & Contact Harvesting — Design Spec

- **Data**: 2026-06-06
- **Branch di partenza**: `feature/import-profiles` (mergiato/aggiornato → nuovo branch `feature/advanced-scraping`)
- **Stato**: Approvato (design) — pronto per il piano implementativo
- **Autore**: Claude (brainstorming con l'utente)

---

## 1. Contesto e motivazione

BOT OUTBOUND nasce come bot di outreach DM su Instagram. Un cliente chiede una
**funzionalità di scraping avanzato orientata alla lead-generation**: raccogliere
informazioni di contatto complete dei profili (non solo nome + bio), con o senza
invio di messaggi. Caso d'uso: vendere/usare liste di lead (negozi di abbigliamento)
con numero di telefono, email, WhatsApp e tutti i link in bio.

Due esigenze derivate, da soddisfare nello stesso lavoro:

1. **Dati di contatto universali** — telefono, email, WhatsApp, tutti i link bio
   devono essere estratti e salvati **ad ogni scraping**, sia in modalità lead-only
   sia nelle campagne di messaggistica esistenti (dato in più sempre utile, es.
   chiamate a freddo senza ri-scrapare).
2. **Messaggistica opzionale** — una campagna deve poter fare **solo scraping**,
   lasciando i campi messaggio vuoti, con un toggle attivabile/disattivabile anche
   dopo che lo scraping è già completato.

Il vincolo dominante resta l'**anti-ban Instagram**, particolarmente delicato sullo
scraping ad alto volume.

### Fatto chiave che vincola i numeri (verificato nel codice)

Il dato avanzato **non aumenta il carico verso Instagram**. Sia lo scraper
(`scraper.py`) sia il resolver import (`import_resolver.py`) già chiamano
`user_info_by_username_v1`, che colpisce `users/{username}/usernameinfo/` e
costruisce `User(**data)` (instagrapi `extractors.extract_user_v1`). La risposta
contiene già — per i profili **business pubblici** — `public_email`,
`public_phone_number`, `public_phone_country_code`, `contact_phone_number`,
`bio_links[]`, `category`, `address_street`, `city_name`. Oggi vengono **scartati**.
Estrarre i contatti = mappare campi già scaricati. **Stessa identica call IG**, zero
chiamate aggiuntive. Il collo di bottiglia resta il numero di `user_info` al giorno.

---

## 2. Obiettivi e non-obiettivi

### Obiettivi (Fase 1 — questa spec)

- Estrarre e persistere **telefono, email, WhatsApp, tutti i link bio** da ogni
  scraping (campo business strutturato IG **+ regex sul testo bio**).
- Salvare i dati di contatto **universalmente**: su `followers` e su
  `global_contacts`, per scraping E per messaggistica.
- Rendere la **messaggistica opzionale** per campagna (toggle `messaging_enabled`,
  template nullable, attivabile anche post-scraping).
- Lead-only: ogni profilo scrapato entra in `global_contacts` come **lead visto**
  (mai contattato), per dedup cross-campagna ed export unico.
- **Merge** dei dati di contatto cross-campagna in `global_contacts` (tiene il dato
  migliore + traccia la provenienza).
- **Cap anti-ban scraping per-account** configurabile (lookup/giorno), indipendente
  dal cap DM.
- **Export** lead esteso con i nuovi campi + **filtri multi-select per campagna e
  per account scraping** (per non esportare contatti di altri clienti/non pagati) +
  filtri "solo con telefono" / "solo con email".

### Non-obiettivi (Fase 2 — spec separata)

- Scraping del **sito web** linkato in bio per estrarre contatti mancanti
  (regex-first, AI fallback). Outline in §13, ma **non implementato qui**.

### Esplicitamente fuori scope

- Backfill dei follower già scrapati (decisione utente: solo da ora in avanti).
- Multi-campagna parallela (Fase 7B, indipendente).
- Verifica/validazione esterna di email (bounce check) e numeri.

---

## 3. Decisioni di design (dal brainstorming)

| # | Decisione | Scelta |
|---|---|---|
| 1 | Modalità solo-scraping | **Toggle `messaging_enabled` su ogni campagna** (no tipo separato) |
| 2 | Dove salvare i contatti | **`followers` + `global_contacts`** (universale) |
| 3 | Web enrichment | **Fase 2**, dopo il core |
| 4 | Metodo web (Fase 2) | **Regex first, AI solo fallback** |
| 5 | Lead in global_contacts senza DM | **Sì**, come "lead visto" senza marcare contattato |
| 6 | Cap scraping/account | **Sì**, cap lookup/giorno configurabile |
| 7 | Backfill vecchi follower | **No**, solo da ora in avanti |
| 8 | Export | **Estendo CSV leads + filtri multi-select per campagna/account scraping** |
| 9 | Regex su testo bio | **Sì**, già in Fase 1 (oltre ai campi business) |
| 10 | Stato finale campagna lead-only | **`completed`** (lead pronti, nessun DM) |
| 11 | Stesso profilo in 2 campagne | **Merge**: tiene il dato migliore + traccia provenienza |

---

## 4. Architettura — vista d'insieme

```
                       ┌─────────────────────────┐
   user_info (IG)  →   │  contact_extract.py      │  ← NUOVO modulo puro
   (1 call, già fatta) │  extract_contacts(info)  │
                       │  → ContactData           │
                       └───────────┬─────────────┘
                                   │ (phone, email, whatsapp, bio_links, …)
              ┌────────────────────┼────────────────────┐
              ▼                                          ▼
     scraper.py (_scrape_paginated            import_resolver.py
       bio-fetch per follower)                  (_resolve_one)
              │                                          │
              ▼                                          ▼
        Follower(+contatti)                       Follower(+contatti)
              │                                          │
              └──────────────┬───────────────────────────┘
                             ▼
            global_contacts upsert+merge (lead visto / contattato)
                             │
                             ▼
            leads.py export (colonne contatto + filtri multi-select)
```

Il modulo `contact_extract.py` è **puro e testabile** (input: oggetto user_info o
dict-like; output: dataclass `ContactData`). Nessuna dipendenza da DB/rete →
unit-test deterministici. È l'unico punto in cui vive la logica di estrazione, usato
da entrambi i percorsi (scrape + import) per garantire parità di comportamento
(regola anti-divergenza di `CLAUDE.md`).

---

## 5. Modello dati

### 5.1 Migrazione 014 (Alembic, contro Supabase Postgres)

Tutte le colonne nuove sono **nullable** o con default, niente enum nativi. La
migrazione deve essere applicata con il bot fermo (vedi nota Supabase in `CLAUDE.md`
su lock `idle in transaction`).

### 5.2 `followers` — nuove colonne

| Colonna | Tipo | Default | Note |
|---|---|---|---|
| `phone` | `String(64)` | null | numero normalizzato (E.164 se possibile) |
| `email` | `String(255)` | null | email lowercased |
| `whatsapp` | `String(255)` | null | numero o link `wa.me`/`api.whatsapp.com` |
| `bio_links` | `Text` (JSON) | `null` | **tutti** i link bio: `[{"url":…,"title":…}]` |
| `contact_source` | `Text` (JSON) | `null` | provenienza per campo: `{"phone":"ig_business"|"bio_regex", …}` (debug/qualità) |
| `contact_extra` | `Text` (JSON) | `null` | slot riservato Fase 2 (dati dal sito web) |

`external_url` resta invariato (primo link, backcompat). `bio_links` è la fonte
completa.

### 5.3 `global_contacts` — nuove colonne

| Colonna | Tipo | Default | Note |
|---|---|---|---|
| `phone` | `String(64)` | null | merge cross-campagna |
| `email` | `String(255)` | null | merge |
| `whatsapp` | `String(255)` | null | merge |
| `bio_links` | `Text` (JSON) | `null` | merge (unione link distinti) |
| `external_url` | `String(512)` | null | oggi assente — `leads.py` lo aggrega via `func.max(Follower.external_url)`; lo materializziamo |
| `contact_extra` | `Text` (JSON) | `null` | Fase 2 |
| `scrape_sources` | `Text` (JSON) | `"[]"` | `[{campaign_id, campaign_name, scraping_account_id, scraping_account_username, scraped_at}]` — alimenta i filtri export |
| `first_seen_at` | `DateTime` | null | prima volta visto come lead (anche se mai contattato) |

`last_contacted_at = null` + `first_seen_at != null` ⇒ **lead visto, mai
messaggiato**. La dedup d'invio resta a send-time, invariata.

### 5.4 `campaigns` — nuove colonne / modifiche

| Colonna | Tipo | Default | Note |
|---|---|---|---|
| `messaging_enabled` | `Boolean` | `True` | il toggle. `True` = comportamento attuale |
| `scrape_daily_limit` | `Integer` | null | override per-campagna del cap lookup/giorno/account (`.env` default se null) |
| `base_message_template` | `Text` | **→ nullable** | era `NOT NULL`. Ora ammette campagna senza messaggio |

> Backcompat: campagne esistenti hanno `messaging_enabled` default `True` e template
> già valorizzato → nessun cambio di comportamento.

### 5.5 Conteggio lookup giornaliero per account

Per il cap scraping serve un contatore lookup/giorno/account. Si rispecchia il
pattern del daily reset DM già presente in `account_manager.py`.

- **Opzione scelta**: nuove colonne su `instagram_accounts`:
  `scrape_lookups_today` (`Integer`, default 0) + `scrape_lookups_reset_at`
  (`DateTime`, null). Reset giornaliero nello stesso `daily_reset` esistente.
- Motivazione: evita query pesanti su `activity_logs`; identico al meccanismo DM
  count, basso rischio, coerente con il codice esistente.

---

## 6. Componente: `app/utils/contact_extract.py` (NUOVO)

Modulo puro. Nessun import da DB/rete.

### 6.1 Contratto

```python
@dataclass
class ContactData:
    phone: str | None
    email: str | None
    whatsapp: str | None
    bio_links: list[dict]          # [{"url": str, "title": str|None}]
    external_url: str | None       # primo link (backcompat)
    sources: dict[str, str]        # {"phone": "ig_business"|"bio_regex", ...}

def extract_contacts(info) -> ContactData: ...
```

`info` = oggetto `instagrapi.types.User` (o dict-like, per i test).

### 6.2 Logica

1. **Campi business strutturati IG** (priorità alta):
   - `phone` ← `public_phone_number` con `public_phone_country_code` (compone
     E.164: `+{cc}{number}`), fallback `contact_phone_number`.
   - `email` ← `public_email`.
   - `bio_links` ← `info.bio_links` (lista `BioLink` → `{"url","title"}`), unione
     con `external_url` se non già presente.
2. **Regex sul testo bio + sui link** (priorità bassa, riempie i buchi):
   - **Email**: regex standard `[\w.+-]+@[\w-]+\.[\w.-]+` sul testo `biography`.
   - **Telefono**: regex numeri telefonici (gestire prefissi `+`, spazi, `/`, `-`,
     parentesi; lunghezza minima ragionevole per evitare falsi positivi su anni/PIVA).
   - **WhatsApp**: cerca `wa.me/<num>`, `api.whatsapp.com/send?phone=<num>`,
     `chat.whatsapp.com/...` nei `bio_links` e nel testo bio. Se trovato numero →
     popola anche `whatsapp` (e `phone` se mancante).
3. **Merge priorità**: strutturato vince su regex. `sources` registra da dove arriva
   ogni campo (qualità/debug, utile per filtrare lead "deboli" in futuro).
4. **Normalizzazione**: email lowercase/trim; telefono strip caratteri non
   numerici tranne `+` iniziale; dedup link per URL normalizzato.

### 6.3 Robustezza

- Mai solleva: ogni accesso a campi `info` via `getattr(..., None)`; regex in
  try/except locale; ritorna `ContactData` con campi null in caso di input sporco.
- Falsi positivi telefono: soglia minima cifre (es. ≥ 8) + scarto pattern chiari di
  P.IVA/anni quando isolati. Documentato nei test.

### 6.4 Test (unit, deterministici)

- Business con tutti i campi → estrae phone E.164 + email + bio_links multipli.
- Personale con email scritta in bio → regex la prende; phone null.
- Bio con `wa.me/39333...` → whatsapp + phone.
- Bio rumorosa (anno "2024", P.IVA) → niente falso telefono.
- `bio_links` multipli → tutti preservati, dedup.
- Input null/incompleto → `ContactData` vuoto, nessuna eccezione.

---

## 7. Integrazione scraper (`scraper.py`)

Nel punto di bio-fetch per follower dentro `_scrape_paginated` (dove oggi si
costruisce `Follower` con biography/external_url), chiamare
`extract_contacts(info)` e popolare i nuovi campi `phone/email/whatsapp/bio_links/
contact_source`. Subito dopo la creazione/aggiornamento del `Follower`, eseguire
l'**upsert+merge** in `global_contacts` come *lead visto* (vedi §9), anche quando
`messaging_enabled=False`.

Nessun cambiamento al timing, al session break, all'ordine randomizzato. Si aggiunge
solo il **decremento del budget lookup** e il check del cap (vedi §10).

---

## 8. Integrazione import resolver (`import_resolver.py`)

Stesso trattamento dello scraper, per parità di comportamento: in `_resolve_one` /
nel punto di creazione `Follower`, chiamare `extract_contacts(info)` e popolare i
campi contatto + upsert `global_contacts`. Il cap lookup (§10) si applica anche qui
(il resolver fa `user_info_by_username_v1`, identica al carico scraper).

---

## 9. `global_contacts`: lead visto + merge

### 9.1 Upsert "lead visto" (a scrape-time)

Per ogni follower scrapato/risolto, upsert su `global_contacts` (chiave
`ig_user_id`):

- Se **non esiste**: crea riga con dati profilo + contatti, `first_seen_at = now`,
  `last_contacted_at = null`, `scrape_sources = [questa sorgente]`.
- Se **esiste**: **merge** (§9.2) + append a `scrape_sources` la coppia
  `(campaign, scraping_account)` se non già presente.

Questo è additivo e indipendente dalla dedup d'invio esistente (che resta a
send-time e popola `contact_history`/`last_contacted_at`).

### 9.2 Regola di merge (cross-campagna)

Per ciascun campo contatto (`phone`, `email`, `whatsapp`, `external_url`):

- Se il valore esistente è **null/vuoto** e il nuovo è presente → **prendi il
  nuovo**.
- Se entrambi presenti → **mantieni l'esistente** (stabilità), salvo che il nuovo
  provenga da fonte a priorità maggiore (`ig_business` > `bio_regex`) registrata in
  `contact_source`.
- `bio_links`: **unione** dei link distinti (per URL normalizzato).
- `biography`, `full_name`, `username`: aggiorna sempre all'ultimo (già fatto oggi a
  send-time in `campaign_orchestrator.py`; replichiamo nell'upsert scrape-time).

La logica di merge vive in un helper riusabile (es.
`global_contact_service.upsert_lead(db, info, contacts, campaign, account)`),
chiamato sia dallo scraper sia dal resolver, e compatibile con il path send-time
esistente.

### 9.3 Interazione con send-time esistente

`campaign_orchestrator.py` continua a fare `_mark_globally_contacted` a invio
riuscito (popola `contact_history`, `last_contacted_at`). Aggiunta: quando segna
contattato, aggiorna anche i campi contatto se più completi (stesso merge). Nessuna
regressione sulla dedup invio.

---

## 10. Anti-ban: cap scraping per-account

### 10.1 Config

- Nuova `.env`: `SCRAPE_DAILY_LIMIT` (int, default conservativo **180**) — lookup
  `user_info`/giorno/account.
- Override per-campagna: `campaigns.scrape_daily_limit` (null = usa `.env`).
- Documentare in `.env.example` + `CLAUDE.md`.

### 10.2 Conteggio e reset

- `instagram_accounts.scrape_lookups_today` incrementato ad ogni `user_info`
  riuscita (scrape + resolve).
- Reset giornaliero agganciato al `daily_reset` esistente in `account_manager.py`
  (stessa finestra del reset DM count).

### 10.3 Comportamento al raggiungimento

- Scraper/resolver, prima di ogni `user_info`, verificano il budget dell'account
  corrente.
- Budget esaurito → **ruota** su altro account `scraping`/`both` con budget residuo
  (riusa `_get_fallback_account`), riacquisendo lo slot.
- Nessun account con budget → **pausa** la raccolta fino al reset giornaliero
  (stato/evento coerente con `scraping_break`/`rate_limited`; emette evento e
  ActivityLog). Il cursore scrape è già persistito → ripresa pulita.

### 10.4 Numeri di riferimento (documentazione operatore)

Stime empiriche (non garantite da IG), per `CLAUDE.md` / guida:

| Profilo account | Lookup/giorno sostenibili |
|---|---|
| Nuovo (warm-up) | 50–100 |
| Maturo (>3 mesi) | 150–300 (default 180) |
| Aggressivo | 500–1000 (rischio ban) |

Per IP/connessione: **2–3 account per IP mobile/residenziale**. 1 connessione (es.
SIM mobile) con 2–3 account × ~180 = **~360–540 profili/giorno**. Scala lineare con
connessioni e account.

### 10.5 Invarianti anti-detection (NON modificare)

- Distribuzioni lognormali, mai delay uniformi.
- Session break vincolanti (no aggiramento via recovery/reenqueue).
- `bio_fetch_delay` per-campagna invariato.
- Ordine randomizzato.
- Il cap è un **freno aggiuntivo**, non sostituisce nessuno dei meccanismi esistenti.

---

## 11. Messaggistica opzionale (toggle)

### 11.1 Stato e validazioni

- `messaging_enabled` (default `True`). Form nuova campagna: switch **"Invia
  messaggi"**. Quando OFF: template e contesto AI opzionali; non serve account ruolo
  `dm`/`both` per avviare; basta un account `scraping`/`both`.
- `base_message_template` diventa nullable a livello DB; la validazione applicativa
  richiede template non vuoto **solo se** `messaging_enabled=True`.

### 11.2 Ciclo di vita

```
draft ──start-scrape──▶ scraping ──(fine, messaging OFF)──▶ completed   [lead pronti]
                                     │
                                     └─(fine, messaging ON)──▶ ready ──start──▶ running ...
```

- `messaging_enabled=False` a fine scraping → **`completed`** (lead esportabili).
  Nessun worker DM, nessuna generazione AI (`auto_generate` forzato a `False`).
- Riattivazione: l'utente attiva il toggle (richiede template + account DM) → la
  campagna `completed` può essere portata a `ready`/`running` per l'invio (riusa i
  flussi `start`/`resume` esistenti, con i guard di §11.3).

### 11.3 Guard

- `start` / `start-dm-auto`: se `messaging_enabled=False` → 400 con messaggio chiaro
  ("Messaggistica disattivata per questa campagna").
- `start` con `messaging_enabled=True` ma template vuoto → 400.
- `update_campaign`: passare `messaging_enabled=True` senza template/account DM →
  l'errore arriva al momento di `start`, non blocca il salvataggio del toggle.
- `enqueue_collection` (dispatcher esistente import/scrape) invariato: la modalità
  lead-only è ortogonale a `source_type` (funziona sia con `scrape` sia con
  `import`).

---

## 12. Export lead + filtri (`leads.py`, frontend)

### 12.1 CSV

Estendere l'export esistente con colonne: `phone`, `email`, `whatsapp`,
`bio_links` (join leggibile, es. `url1 | url2`). Mantenere le colonne attuali.

### 12.2 Filtri (requisito esplicito utente)

- **Multi-select per campagna** e **per account scraping** (da `scrape_sources`):
  l'utente seleziona una o più campagne/profili → esporta solo quei lead. Scopo:
  non esportare contatti non pagati o di altri clienti.
- Filtri booleani: **solo con telefono**, **solo con email**, (già esistenti:
  has_replied, verified_only, min_followers).
- I filtri si applicano sia alla **lista** sia all'**export CSV** (riuso del pattern
  `_build_conditions` già rifattorizzato per la subquery, evitando il bug 500 storico
  in `leads.py`).

### 12.3 UI

- Pagina leads: nuove colonne contatto (telefono/email/whatsapp/link), badge "lead
  non contattato" quando `last_contacted_at` null.
- Controlli filtro multi-select (campagne + account scraping), toggle "solo con
  telefono/email", bottone export rispetta i filtri attivi.

---

## 13. Fase 2 (outline, spec separata — NON implementare ora)

Web/WhatsApp enrichment per i contatti mancanti:

- Worker `enrich_from_website` post-scraping, **nessuna call IG** (dominio diverso,
  zero costo rate-limit Instagram).
- Per ogni lead con `bio_links` e senza phone/email: `httpx GET` della home →
  **regex** su `tel:`/`mailto:`/`wa.me` (costo zero). Se la pagina è confusa →
  **fallback AI** (Groq/Gemini già integrati) su testo ridotto.
- Scrive in `followers.contact_extra` / `global_contacts.contact_extra` +
  promuove a `phone/email/whatsapp` se mancanti (con `contact_source="website"`).
- Concorrenza limitata, timeout, rispetto `robots`/educato. Dettagli nella spec
  Fase 2.

---

## 14. Riepilogo file toccati

### Backend

| File | Modifica |
|---|---|
| `app/utils/contact_extract.py` | **NUOVO** — estrazione pura + regex |
| `app/services/global_contact_service.py` | **NUOVO** (o helper in esistente) — upsert+merge lead |
| `alembic/versions/014_*.py` | **NUOVO** — colonne followers/global_contacts/campaigns + template nullable + scrape lookup count |
| `app/models/follower.py` | nuove colonne contatto |
| `app/models/global_contact.py` | nuove colonne contatto + scrape_sources + first_seen_at |
| `app/models/campaign.py` | `messaging_enabled`, `scrape_daily_limit`, template nullable |
| `app/models/account.py` | `scrape_lookups_today`, `scrape_lookups_reset_at` |
| `app/services/scraper.py` | estrazione contatti + upsert lead + cap lookup/rotazione |
| `app/services/import_resolver.py` | estrazione contatti + upsert lead + cap lookup |
| `app/services/account_manager.py` | cap scraping + reset giornaliero lookup |
| `app/services/campaign_orchestrator.py` | merge contatti a send-time (no regressione dedup) |
| `app/api/campaigns.py` | create/update `messaging_enabled` + `scrape_daily_limit`; guard `start`; stato finale `completed` |
| `app/api/leads.py` | colonne export + filtri multi-select campagna/account + solo-con-telefono/email |
| `app/schemas/campaign.py`, `app/schemas/follower.py`, `app/schemas/lead.py` | nuovi campi |
| `app/config.py`, `.env.example` | `SCRAPE_DAILY_LIMIT` |

### Frontend

| File | Modifica |
|---|---|
| `lib/types.ts`, `lib/api.ts` | nuovi campi + filtri export |
| `components/campaigns/CampaignForm` | toggle "Invia messaggi" + template opzionale + `scrape_daily_limit` |
| `app/campaigns/[id]/page.tsx` | indicazione modalità lead-only / toggle post-scraping |
| `app/leads` (pagina) | colonne contatto, badge non-contattato, filtri multi-select, export filtrato |

### Docs

- `CLAUDE.md` (sezione scraping avanzato + cap + numeri), `INDEX.md`,
  `docs/project/PROGRESS.md` (nuova fase), memoria `project_state.md`.

---

## 15. Piano di test

- **Unit `contact_extract`**: §6.4 (business, personale, whatsapp, falsi positivi,
  link multipli, input sporco).
- **Unit merge `global_contacts`**: campo mancante riempito, campo presente non
  sovrascritto, priorità ig_business > regex, unione bio_links, scrape_sources append
  senza duplicati.
- **Unit cap scraping**: budget decrementa, rotazione a budget esaurito, pausa se
  nessun account, reset giornaliero.
- **Unit/integrazione lifecycle toggle**: messaging OFF → stato `completed`, nessun
  worker DM, AI saltata; guard `start` su template vuoto / messaging OFF.
- **Integrazione export**: filtro multi-select campagna/account isola i lead; "solo
  con telefono" funziona su lista E CSV.
- **Regressione**: suite esistente (≥33 test) verde; dedup invio invariata; campagne
  esistenti (messaging_enabled default True) comportamento immutato.

---

## 16. Migrazione / note operative

- Migrazione 014 contro **Supabase** (`python -m scripts.migrate`). Prima: fermare
  il bot, verificare nessuna sessione `idle in transaction` che locka
  `campaigns`/`followers` (vedi `CLAUDE.md`).
- Tutte le colonne `native_enum=False`/nullable per compatibilità Supabase/Postgres
  (lezione enum nativi di `project_state.md`).
- Riavvio richiesto: backend FastAPI + worker ARQ + cron worker (nuovo codice
  contatti/cap).

---

## 17. Rischi e verifiche

| Rischio | Mitigazione |
|---|---|
| `usernameinfo` non popola i campi business per alcuni profili | Verificato che `extract_user_v1` mappa i campi; per profili senza contatto pubblico è atteso null. Test reale su 1 profilo business prima di costruirci la UI sopra. |
| Falsi positivi regex telefono | Soglia cifre minima + scarto pattern P.IVA/anno; coperto da test dedicati. |
| Carico IG da lookup massivi | Cap per-account + rotazione + pausa a reset; numeri conservativi documentati. |
| Divergenza scraper vs resolver | Logica unica in `contact_extract.py` + helper merge condiviso (regola anti-divergenza `CLAUDE.md`). |
| Export "fuga" contatti tra clienti | Filtri multi-select per campagna/account scraping, applicati anche al CSV. |
| Migrazione bloccata da zombie Supabase | Procedura fermo-bot + check lock documentata. |

---

## 18. Ordine di implementazione suggerito (per il piano)

1. Migrazione 014 + modelli + schemi (fondamenta DB).
2. `contact_extract.py` + test (puro, isolato).
3. Integrazione scraper + resolver (estrazione + upsert lead) + test.
4. Merge `global_contacts` helper + integrazione send-time + test.
5. Cap scraping per-account (config + count + reset + rotazione) + test.
6. Toggle messaggistica (API + lifecycle + guard) + test.
7. Export + filtri (`leads.py`) + test.
8. Frontend (form toggle, colonne/filtri leads).
9. Docs + memoria.

Fase 2 (web enrichment) → spec e piano separati dopo il merge del core.
