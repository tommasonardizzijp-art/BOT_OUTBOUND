# Fallback GraphQL per Fase Bio browser — analisi rischio

Data: 2026-07-29
Contesto: campagna BORDERLINE (id `30f68a3f-9300-46c5-bc67-75805067a694`), motore Fase Bio = `browser` (Patchright).

## 1. Il problema osservato

23 profili su 59 (tutti account business categoria "agenzia scommesse": goldbet/planetwin365/eurobet/snai/better) falliscono la risoluzione bio con lo stesso identico errore, riproducibile al 100%:

```
GET /api/v1/users/web_profile_info/?username=<x>
Header: x-ig-app-id: 936619743392459

→ HTTP 400
{"message":"Asset asset://laser.provider/ig_business_category_subvertical has been deleted. You cannot use this schema","status":"fail"}
```

## 2. Diagnosi effettuata (come si è escluso ogni altro sospetto)

| Ipotesi | Test fatto | Esito |
|---|---|---|
| Account nostro bloccato/checkpoint | Stessa chiamata da 2 account diversi (primero_adv3, borderline_agenzia) | Stesso errore identico da entrambi → non è colpa nostra |
| Sessione non loggata / scaduta | Browser headed reale, navigato al profilo, badge notifiche visibile in pagina | Sessione loggata, pagina carica (status 200) |
| Timing/rate limit | Retry dopo reset a `pending`, in orari diversi | Stesso errore, sempre uguale |
| Bug generico di tutta l'API | Stessi 23 profili sempre e solo loro; gli altri 36 profili (stesse categorie business ma anche non-business) risolvono senza problemi | Errore specifico a un sottoinsieme di account business con una certa "subvertical" di categoria |

**Conclusione**: bug lato server Instagram. Un asset interno (`ig_business_category_subvertical`) usato per arricchire la risposta di `web_profile_info` per certi account business è stato rimosso/rotto nel loro backend, e l'intera risposta fallisce con 400 invece di degradare silenziosamente. Non è un blocco/checkpoint verso di noi, non dipende da quale account chiama, non dipende da IP/proxy.

## 3. Perché la pagina si apre comunque (il paradosso che Tommaso ha notato giustamente)

Aprendo il profilo in un browser vero, il profilo si vede benissimo: foto, nome, bio, follower — tutto presente visivamente. Questo è **coerente** col bug, non contraddice: la pagina web di Instagram NON usa `web_profile_info` per renderizzare sé stessa. Usa un endpoint diverso, **GraphQL** (`/api/graphql`, query interna chiamata `PolarisProfilePageContentQuery`), che è un pipeline separato lato IG e non è toccato dal bug.

Verificato concretamente: intercettata la risposta reale di quella query GraphQL per uno dei 23 profili falliti (`planetwinpiromallo`) — risposta 200, contiene lo stesso set di dati che ci serve:

```json
{"data":{"user":{
  "pk":"77905145792","username":"planetwinpiromallo",
  "full_name":"Planetwin Piromallo",
  "biography":"Via conte piromallo 40/42 \nSan sebastiano al vesuvio📍",
  "follower_count":658,"following_count":92,
  "is_private":false,"is_verified":false,
  "external_url":"","bio_links":[],
  ...
}}}
```

Cioè: il DATO esiste ed è raggiungibile, solo non tramite l'endpoint che il nostro codice chiama oggi.

## 4. Cosa fa oggi il codice (per contesto)

File: `app/services/browser_bio.py`, funzione `_capture_web_profile_info` (righe 98-165).

Strategia attuale, in ordine:
1. Apre il profilo nel browser, **ascolta passivamente** le risposte di rete: se durante il caricamento della pagina passa una risposta `web_profile_info` (che il JS di Instagram stesso spara per popolarsi), la cattura — **zero chiamate nostre**, solo lettura di traffico che il browser genera comunque.
2. Se non intercettata entro 8 secondi, fa un **fetch esplicito in-page** (dentro il contesto della pagina, con gli stessi cookie/header che userebbe l'app web) verso lo stesso endpoint.
3. Per questi 23 profili, sia il passo 1 che il passo 2 ricevono lo stesso 400 — perché il bug è nell'endpoint stesso, non nel modo in cui lo chiamiamo.

## 5. Proposta di modifica

Aggiungere un **secondo canale di intercettazione passiva**, parallelo al primo: durante lo stesso caricamento di pagina, ascoltare ANCHE le risposte di `/api/graphql` e, se una contiene i campi che ci servono (`biography`, `follower_count`, `pk`, ecc. dentro `data.user`), usarla come fonte quando `web_profile_info` fallisce con quello specifico errore.

Punto chiave della proposta: **NON costruire noi la richiesta GraphQL**. La richiesta la fa il browser da solo, navigando (è lì che l'ho vista, nello stesso caricamento pagina che oggi già facciamo). Noi ci limitiamo a leggerla, esattamente come già facciamo con `web_profile_info` al passo 1.

Perché questo distinguo è cruciale per il rischio (vedi sezione 6): costruire quella richiesta a mano vorrebbe dire replicare token di sessione che Instagram ruota e verifica (`fb_dtsg`, `lsd`, `doc_id` legato alla versione del loro frontend) — fragile e, quello sì, un pattern anomalo. Intercettarla passivamente da traffico che il browser genera comunque non aggiunge nessuna chiamata e nessun pattern nuovo.

## 6. Analisi rischio dettagliata

### 6.1 Volume/frequenza di chiamate verso Instagram
**Nessun aumento.** Le chiamate GraphQL (`PolarisProfilePageContentQuery` e le altre ~12 viste nella cattura: query post, storie, suggerimenti, ecc.) le fa il browser di Instagram stesso ad ogni caricamento profilo, sempre, per qualunque utente loggato — bot o umano. Oggi il nostro codice le lascia già passare senza leggerle. La modifica aggiunge solo un listener, non una richiesta.

### 6.2 Fingerprint/pattern di rete
**Nessun cambiamento del traffico in uscita.** Chi osserva il traffico di rete (IG lato server) vede esattamente lo stesso identico set di richieste, nello stesso ordine, con gli stessi header, prima e dopo la modifica. L'unica differenza è INTERNA al nostro processo: cosa facciamo con una risposta che riceviamo comunque.

### 6.3 Fragilità/manutenzione (non è un rischio di sicurezza, ma va dichiarato)
GraphQL è l'endpoint interno del client web IG, non un'API pubblica stabile:
- Il nome della query e la sua struttura possono cambiare a un deploy futuro di Instagram (il campo `data.user.biography` potrebbe rinominarsi o spostarsi).
- Se cambia, il parsing smette semplicemente di trovare i campi attesi → il fallback non scatta, il profilo torna a fallire come fa oggi. **Nessun peggioramento rispetto allo stato attuale**, solo un fallback che a un certo punto potrebbe smettere di funzionare e richiedere un aggiornamento del mapping.
- Non richiede credenziali/token gestiti da noi: leggiamo solo risposte già transitate nella sessione browser autenticata normalmente.

### 6.4 Rischio account (ban/challenge/soft-block)
**Nullo aggiuntivo.** La sequenza di navigazione (apri profilo, aspetta il caricamento, eventuale scroll) resta identica bit-per-bit a quella già in uso su migliaia di profili finora. Non cambia frequenza, non cambia user-agent/fingerprint, non cambia nessun parametro osservabile da IG.

### 6.5 Copertura dati
Il payload GraphQL osservato contiene tutti i campi che oggi mappiamo dal web_profile_info: `pk`, `username`, `full_name`, `biography`, `follower_count`, `following_count`, `is_private`, `is_verified`, `external_url`, `bio_links`. Non contiene `business_email`/`business_phone_number` diretti — ma quei due campi oggi **non arrivano comunque da web_profile_info** (torna sempre null lì), arrivano da un endpoint terzo separato (`/api/v1/users/{pk}/info/`) già usato oggi in `_fetch_public_contact_inpage`. Quindi nessuna perdita di dati rispetto allo stato attuale.

### 6.6 Cosa NON sto proponendo (per chiarezza)
- Non propongo di chiamare l'endpoint GraphQL attivamente (fetch in-page) come si fa oggi al passo 2 per web_profile_info — solo intercettazione passiva del passo 1. Se anche l'intercettazione passiva fallisse (nessuna risposta GraphQL catturata in tempo), il profilo resta semplicemente irrisolto come oggi, senza tentare un fetch esplicito rischioso.
- Non propongo di applicare questo fallback su TUTTI i profili sempre — solo come fallback quando `web_profile_info` risponde con l'errore 400 specifico di questo bug (o comunque quando fallisce), lasciando invariato il flusso principale per il 99% dei casi che oggi funziona già.

## 7. Conclusione e raccomandazione

Il fallback proposto non introduce alcun rischio nuovo lato anti-detection/ban: usa dati che il browser genera comunque durante la navigazione normale, senza aggiungere richieste. Il solo costo reale è manutentivo (uno schema interno di IG che può cambiare senza preavviso), mitigato dal fatto che il fallback degrada in modo sicuro (torna al comportamento — non peggiore di — quello attuale).

Va soppesato però il fatto che si tratta di leggere un endpoint **non documentato/non pubblico** di Instagram (a differenza di `web_profile_info`, che pur non essendo un'API ufficiale è quantomeno lo standard de-facto usato da innumerevoli tool di terze parti da anni). GraphQL interno cambia più spesso e più silenziosamente. Questo è un tradeoff tra "risolvere subito i 23 profili bloccati" e "aggiungere una dipendenza tecnica meno stabile nel tempo" — non un tradeoff di sicurezza/rischio account.

## 8. Verifica live (2026-07-30, probe reale)

Implementato in `feat/graphql-fallback-bio` (PR #24). Probe `backend/scripts/probe_graphql_fallback.py` girata su **tutti i 36 follower reali** della campagna "BORDERLINE X PROFILI SCOMMESSE 1" (`30f68a3f-...`), account `borderline_agenzia` (dc08807c), chromium-1208 (default, NON D:), sessione loggata, ritmo umano 6s.

| Metrica | Valore |
|---|---|
| Profili testati | 36/36 |
| Risolti | **36 (100%)** |
| Falliti | 0 |
| Recuperati via fallback GraphQL | **2** (`goldbet_sorgenti_livorno`, `agenzia_giuliocesare`) |
| Risolti via web_profile_info (path primario invariato) | 34 |
| Regressioni | 0 |

**Scoperta:** il bug server IG (`asset ig_business_category_subvertical deleted`) è **INTERMITTENTE**. Il 29/07 colpiva 23/59 (~39%); il 30/07 solo ~2/36 (~6%). Quindi:
- Ipotesi FALLBACK confermata: ogni profilo colpito dal 400 recupera via GraphQL (2/2), pk+follower_count+biography estratti correttamente.
- Ipotesi "GraphQL emessa passivamente" confermata su profili DIVERSI da quello dell'audit (planetwinpiromallo) → non era un caso isolato.
- Path primario intatto: 34 profili sani risolti da web_profile_info senza toccare il fallback.

**Nota per il criterio "≥5/6":** non raggiungibile stanotte perché il bug non stava firing a volume, NON per un difetto del codice. Il campione del path-fallback è piccolo (2) proprio perché il bug è quiescente. Il valore pieno del fallback si vedrà al prossimo flare del bug — vale ri-girare la probe durante un flare attivo per osservare il recupero multi-profilo.

**Caveat operativo:** l'account `borderline_agenzia` è partito SENZA proxy (warning nei log) → traffico da IP locale. Per la probe read-only è ininfluente, ma in produzione va configurato il proxy dell'account.
