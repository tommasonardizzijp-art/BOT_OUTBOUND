# Lead Qualification - Design

Data: 2026-06-08

## Obiettivo

Creare una sezione dedicata del bot per qualificare i lead gia presenti in
`global_contacts` rispetto a target descritti in linguaggio naturale.

L'utente deve poter:

- salvare target riutilizzabili;
- descrivere liberamente il tipo di lead desiderato;
- ricevere dall'AI una proposta modificabile di criteri/keyword;
- applicare la classificazione solo a un sottoinsieme filtrato di lead;
- evitare run troppo grandi senza conferma/limite;
- usare scoring deterministico sulla massa;
- usare AI solo sui casi ambigui;
- esportare CSV qualificati da una sezione dedicata;
- confrontare target diversi sullo stesso lead;
- ricevere notifica Telegram a fine run.

La feature lavora solo su `global_contacts`, non sui `followers` grezzi.

## Decisioni dal brainstorming

- Target salvabili: si.
- Criteri AI modificabili manualmente prima della run: si.
- Input target: box libero, con suggerimenti/placeholder guidati.
- Sorgente dati: solo `global_contacts`.
- Prefiltri: data scraping, campagne, account scraping, telefono/email,
  min follower, max lead da processare.
- Limite sicurezza: si, obbligatorio.
- Stati classificazione: `match`, `no_match`, `ambiguous`.
- Soglia export iniziale: `final_score >= 80` per match pulito.
- AI: solo sui lead ambigui.
- Motivo dettagliato non obbligatorio in UI, ma utile salvarlo per audit/debug.
- Run successive: storico preservato; vista operativa mostra il risultato piu
  recente per target+lead.
- Skip: se lead gia qualificato con lo stesso target e stessa versione regole,
  viene saltato di default.
- CSV: sezione dedicata, con grado di confidenza.
- UI: pagina dedicata, non dentro la pagina Leads esistente.
- Multi-target: si, lo stesso lead puo avere qualifiche diverse per target diversi.
- Velocita/precisione: compromesso medio.
- Lingue: italiano + inglese, inclusi termini ibridi tipo boutique, store, retail.
- Sito web esterno: futuro, non MVP.
- Categorie sensibili: nessun trattamento speciale nel MVP.
- Notifica Telegram a fine run: si.

## Approccio scelto

Approccio ibrido:

1. L'utente descrive il target in linguaggio naturale.
2. L'AI compila la descrizione in regole strutturate modificabili.
3. Il sistema seleziona i lead da `global_contacts` usando filtri DB.
4. Lo scoring deterministico assegna punteggio e stato preliminare.
5. Solo i lead ambigui passano all'AI classifier.
6. Il risultato finale viene salvato in DB.
7. La nuova pagina mostra run, risultati, filtri ed export CSV dedicato.

Questo evita di usare AI su 100.000 contatti e mantiene risultati spiegabili.

## Concetto chiave

Non esiste una categoria globale unica del lead.

La domanda corretta e:

> Questo lead e buono per questo target?

Quindi `@negozio_xyz` puo essere:

- `match` per "CBD shop retail";
- `no_match` per "boutique abbigliamento donna";
- `ambiguous` per "ecommerce benessere".

## Modello dati proposto

### `lead_target_profiles`

Target riutilizzabili creati dall'utente.

Campi:

- `id`: UUID string.
- `name`: nome breve, es. "Boutique abbigliamento retail".
- `description`: descrizione libera inserita dall'utente.
- `compiled_rules`: JSON generato dall'AI e modificabile dall'utente.
- `rules_hash`: hash stabile delle regole normalizzate.
- `pass_threshold`: default 80.
- `reject_threshold`: default 25.
- `ai_review_min_score`: default 26.
- `ai_review_max_score`: default 79.
- `max_run_size`: default 5000.
- `created_at`.
- `updated_at`.

### `lead_qualification_runs`

Singola esecuzione batch.

Campi:

- `id`: UUID string.
- `target_profile_id`.
- `filters`: JSON con filtri usati.
- `rules_hash`: copia dell'hash regole al momento della run.
- `status`: `queued`, `running`, `completed`, `failed`, `cancelled`.
- `total_candidates`: lead trovati dai filtri.
- `skipped_existing`: lead saltati per stessa target+rules_hash.
- `processed_count`.
- `matched_count`.
- `no_match_count`.
- `ambiguous_count`.
- `ai_reviewed_count`.
- `error_count`.
- `started_at`.
- `completed_at`.
- `created_at`.

### `lead_qualifications`

Risultato per lead e target.

Campi:

- `id`: UUID string.
- `global_contact_id`: FK verso `global_contacts.id`.
- `ig_user_id`: copia utile per join/export.
- `target_profile_id`.
- `run_id`.
- `rules_hash`.
- `deterministic_score`: 0-100.
- `ai_score`: 0-100 nullable.
- `final_score`: 0-100.
- `status`: `match`, `no_match`, `ambiguous`, `error`.
- `matched_signals`: JSON.
- `negative_signals`: JSON.
- `ai_used`: bool.
- `ai_label`: string nullable.
- `reason`: testo breve nullable.
- `model_used`: string nullable.
- `created_at`.

Indici consigliati:

- `(target_profile_id, global_contact_id, rules_hash)`.
- `(target_profile_id, status, final_score)`.
- `(run_id)`.
- `(ig_user_id)`.

Nota: non serve unique rigida su target+lead, per preservare lo storico. Per
saltare le ripetizioni si cerca l'ultimo risultato con stesso
`target_profile_id`, `global_contact_id`, `rules_hash`.

## `compiled_rules` JSON

Esempio:

```json
{
  "target_label": "clothing_retail_store",
  "language_hints": ["it", "en"],
  "positive_terms": [
    "abbigliamento",
    "moda",
    "boutique",
    "fashion",
    "clothing",
    "store",
    "shop"
  ],
  "strong_terms": [
    "negozio",
    "retail",
    "boutique",
    "showroom",
    "shop online",
    "ecommerce"
  ],
  "negative_terms": [
    "wholesale",
    "grossista",
    "B2B",
    "fornitore",
    "influencer",
    "blogger",
    "model"
  ],
  "positive_concepts": [
    "vende abbigliamento al dettaglio",
    "negozio fisico o ecommerce retail",
    "boutique o multibrand"
  ],
  "negative_concepts": [
    "solo influencer",
    "solo contenuti editoriali",
    "grossista o fornitore B2B"
  ],
  "field_weights": {
    "username": 8,
    "full_name": 12,
    "biography": 30,
    "external_url": 15,
    "bio_links": 15,
    "contact_fields": 5,
    "scrape_source": 5
  },
  "score_rules": {
    "strong_term_bonus": 18,
    "positive_term_bonus": 8,
    "negative_term_penalty": 25,
    "external_url_bonus": 8,
    "contact_available_bonus": 4
  }
}
```

## Generazione criteri con AI

Endpoint proposto:

`POST /api/lead-qualification/profiles/compile`

Input:

```json
{
  "description": "Voglio negozi di abbigliamento retail, boutique, showroom e ecommerce. Escludi grossisti, B2B, influencer e fashion blogger."
}
```

Output:

```json
{
  "name_suggestion": "Boutique abbigliamento retail",
  "compiled_rules": { "...": "..." },
  "pass_threshold": 80,
  "reject_threshold": 25,
  "ai_review_min_score": 26,
  "ai_review_max_score": 79
}
```

Prompt design:

- L'AI deve rispondere solo JSON.
- Deve generare keyword IT/EN.
- Deve distinguere segnali positivi forti/deboli e segnali negativi.
- Deve includere sinonimi, termini italianizzati e varianti comuni su Instagram.
- Non deve inventare categorie operative non richieste.

## Scoring deterministico

Input lead:

- `username`.
- `full_name`.
- `biography`.
- `external_url`.
- `bio_links`.
- `phone`, `email`, `whatsapp`.
- `scrape_sources` solo come segnale secondario.
- Statistiche follower derivate dalla join con `followers`, se gia disponibili.

Normalizzazione:

- lowercase;
- rimozione punteggiatura morbida;
- spazi normalizzati;
- match frasi multi-parola;
- match termini con confini semplici;
- gestione IT/EN.

Output deterministico:

```json
{
  "score": 84,
  "status": "match",
  "matched_signals": [
    {"field": "biography", "term": "boutique", "weight": 18},
    {"field": "external_url", "term": "shop", "weight": 8}
  ],
  "negative_signals": []
}
```

Regola iniziale:

- score `>= pass_threshold`: `match`.
- score `<= reject_threshold`: `no_match`.
- score tra `ai_review_min_score` e `ai_review_max_score`: `ambiguous`.

## AI sui casi ambigui

Solo i lead con stato preliminare `ambiguous` passano all'AI.

Endpoint interno/servizio:

`classify_ambiguous_lead(profile, lead, deterministic_result)`.

Prompt:

- Include target description.
- Include compiled rules.
- Include dati lead.
- Include segnali deterministici trovati.
- Chiede risposta JSON rigida:

```json
{
  "status": "match",
  "confidence": 0.82,
  "label": "clothing_retail_store",
  "reason": "Bio e nome indicano una boutique retail; nessun segnale B2B."
}
```

Conversione:

- `confidence * 100` diventa `ai_score`.
- Se AI dice `match` e `confidence >= 0.70`, finale `match`.
- Se AI dice `no_match` e `confidence >= 0.70`, finale `no_match`.
- Se confidence bassa, resta `ambiguous`.

## Filtri pre-run

La run deve partire sempre da un filtro esplicito.

Filtri MVP:

- `date_from`, `date_to` su scrape date (`first_seen_at`, fallback `created_at`).
- `campaign_ids`.
- `scraping_account_ids`.
- `has_phone`.
- `has_email`.
- `min_followers`.
- `max_leads`.
- `skip_existing_same_rules`: default true.

Il backend deve calcolare una stima prima dell'avvio:

`POST /api/lead-qualification/runs/estimate`

Output:

```json
{
  "candidate_count": 4210,
  "already_qualified_same_rules": 830,
  "will_process": 3380,
  "over_limit": false,
  "max_run_size": 5000
}
```

Se `will_process > max_run_size`, il backend rifiuta la run salvo override
esplicito in futuro. Nel MVP meglio rifiutare e chiedere filtri piu stretti.

## API proposte

### Target profiles

- `GET /api/lead-qualification/profiles`
- `POST /api/lead-qualification/profiles/compile`
- `POST /api/lead-qualification/profiles`
- `GET /api/lead-qualification/profiles/{id}`
- `PUT /api/lead-qualification/profiles/{id}`
- `DELETE /api/lead-qualification/profiles/{id}`

### Runs

- `POST /api/lead-qualification/runs/estimate`
- `POST /api/lead-qualification/runs`
- `GET /api/lead-qualification/runs`
- `GET /api/lead-qualification/runs/{id}`
- `POST /api/lead-qualification/runs/{id}/cancel` (futuro o MVP se semplice)

### Results

- `GET /api/lead-qualification/results`
- `GET /api/lead-qualification/results/export`

Parametri results/export:

- `target_profile_id`.
- `run_id` opzionale.
- `status`.
- `min_score`.
- `date_from`, `date_to`.
- `campaign_ids`.
- `scraping_account_ids`.
- `has_phone`.
- `has_email`.
- `page`, `page_size`.

## Worker batch

Nuovo ARQ task:

`qualify_leads_task(ctx, run_id: str)`.

Comportamento:

1. Carica run e target profile.
2. Ricostruisce query `global_contacts` dai filtri salvati.
3. Applica skip stesso target+rules_hash se richiesto.
4. Processa in batch, ad esempio 100 lead alla volta.
5. Salva risultati progressivamente.
6. Aggiorna contatori run.
7. Per lead ambigui chiama AI con concorrenza limitata.
8. A fine run invia notifica Telegram.

Concorrenza AI:

- iniziale: 2 richieste simultanee massimo.
- retry con backoff riusando helper esistenti dove sensato.

Timeout:

- job timeout superiore al worker DM, per batch lunghi, oppure task short-lived
  riaccodato. Nel piano operativo va scelta una delle due strategie.

## UI proposta

Nuova pagina:

`frontend/app/lead-qualification/page.tsx`

Sidebar:

- voce "Qualifica lead".

Sezioni:

1. Target profiles
   - lista target salvati;
   - crea nuovo;
   - modifica criteri.

2. Crea target
   - box descrizione libero;
   - placeholder/suggerimenti:
     - "Che tipo di attivita vuoi includere?"
     - "Che cosa vuoi escludere?"
     - "Retail, B2B, ecommerce, local business?"
     - "Lingua/mercato?"
   - bottone "Genera criteri".

3. Anteprima criteri
   - keyword positive;
   - segnali forti;
   - keyword negative;
   - soglie;
   - editor JSON o campi semplici.

4. Filtri run
   - periodo scraping;
   - campagne;
   - account scraping;
   - solo con telefono/email;
   - min follower;
   - max lead.

5. Stima
   - lead candidati;
   - gia qualificati;
   - processati effettivi;
   - avviso limite.

6. Run
   - stato;
   - progress bar;
   - contatori match/no_match/ambiguous;
   - AI reviewed;
   - errori.

7. Risultati
   - tabella lead qualificati;
   - confidence;
   - stato;
   - contatti;
   - export CSV dedicato.

## CSV export dedicato

Campi consigliati:

- `ig_user_id`.
- `username`.
- `full_name`.
- `biography`.
- `phone`.
- `email`.
- `whatsapp`.
- `external_url`.
- `bio_links`.
- `target_profile`.
- `qualification_status`.
- `confidence_score`.
- `deterministic_score`.
- `ai_score`.
- `ai_used`.
- `matched_signals`.
- `negative_signals`.
- `first_seen_at`.
- `scrape_sources`.
- `scraping_accounts`.

Default export:

- target selezionato obbligatorio;
- `status=match`;
- `min_score=80`.

## Integrazione con codice esistente

Backend:

- Nuovi modelli in `backend/app/models/lead_qualification.py`.
- Nuovi schemi in `backend/app/schemas/lead_qualification.py`.
- Nuovo router in `backend/app/api/lead_qualification.py`.
- Nuovo servizio in `backend/app/services/lead_qualification.py`.
- Nuovo worker in `backend/app/workers/lead_qualification_worker.py` o task in
  `task_queue.py`.
- Nuova migrazione Alembic `015_lead_qualification.py`.
- Registrazione router in `backend/app/main.py`.
- Enqueue helper in `backend/app/services/work_enqueue.py`.
- Uso AI da `app.services.ai_personalizer.get_ai_client`.
- Notifica Telegram via `app.services.notifier`.

Frontend:

- Tipi in `frontend/lib/types.ts`.
- API wrapper in `frontend/lib/api.ts`.
- Pagina dedicata `frontend/app/lead-qualification/page.tsx`.
- Voce sidebar in `frontend/components/layout/Sidebar.tsx`.

## MVP

MVP consigliato:

1. DB: target profiles, runs, qualifications.
2. API CRUD target profile.
3. AI compile target description -> compiled rules.
4. Estimate run.
5. ARQ batch deterministico.
6. AI solo sugli ambiguous.
7. Pagina dedicata con creazione target, filtri, run, risultati.
8. Export CSV dedicato.
9. Telegram a fine run.

## Fuori scope MVP

- Visitare siti web esterni e leggere titolo/meta/body.
- Embeddings/vector search.
- Classificazione su `followers` non ancora consolidati.
- Categorie globali automatiche non legate a target.
- Review manuale dei lead ambiguous.
- Override forzato oltre max run size.
- Cancellazione live robusta di una run gia in corso, se complica troppo.

## Rischi e mitigazioni

### Falsi positivi

Mitigazione:

- negative terms;
- AI solo sugli ambigui;
- soglia export alta (`>=80`);
- export dedicato con confidence visibile.

### Costi AI

Mitigazione:

- prefiltri obbligatori;
- max run size;
- AI solo tra soglie;
- skip same target+same rules.

### Risultati non riproducibili

Mitigazione:

- `rules_hash`;
- salvataggio `compiled_rules`;
- temperature bassa per classifier;
- output JSON validato.

### Run grandi

Mitigazione:

- stima obbligatoria;
- `max_leads`;
- batch commit;
- progress counters.

### Prompt injection da bio lead

Mitigazione:

- trattare bio/link come dati non attendibili;
- prompt con delimitatori;
- output solo JSON;
- validazione schema.

## Domande residue per il piano operativo

1. Valore iniziale `max_run_size`: proposta 5000.
2. Batch size deterministico: proposta 100.
3. Concorrenza AI: proposta 2.
4. Salvare `reason` anche se non mostrato sempre: proposta si.
5. Implementare cancel run nel MVP o lasciarlo futuro: proposta futuro, a meno che
   non sia semplice con controllo status tra batch.

