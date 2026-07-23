# SDD — Canale WhatsApp (BOT OUTBOUND → piattaforma outreach multi-canale)

> Stato: **BOZZA v1 — in attesa di review di Tommaso** · Data: 2026-07-23 · Owner: Tommaso
> Fonti: `00-problematiche-e-decisioni.md` (decisioni chiuse, brainstorming 23/07), `sviluppi-futuri.md` (backlog fase 2+), mappatura repo verificata sul codice.
> Questo documento È l'SDD: descrive **cosa cambia** sulla piattaforma esistente per aggiungere il canale WhatsApp. Non è un design greenfield.
> Dopo la review: spec/plan via workflow superpowers + skill `sviluppo-modulo` per l'MVP.

---

## Indice

1. [Visione e obiettivi](#1-visione-e-obiettivi)
2. [Glossario](#2-glossario)
3. [Vincoli e assunzioni](#3-vincoli-e-assunzioni)
4. [Architettura logica](#4-architettura-logica)
5. [Modello dati](#5-modello-dati)
6. [Componenti: riuso vs adattamento vs nuovo](#6-componenti-riuso-vs-adattamento-vs-nuovo)
7. [Flussi principali (sequence)](#7-flussi-principali-sequence)
8. [State machine](#8-state-machine)
9. [Coesistenza umano ↔ bot](#9-coesistenza-umano--bot)
10. [Threat model e anti-ban](#10-threat-model-e-anti-ban)
11. [Failure mode analysis](#11-failure-mode-analysis)
12. [Appendice GDPR](#12-appendice-gdpr)
13. [PoC gate (go/no-go)](#13-poc-gate-gono-go)
14. [Roadmap a milestone](#14-roadmap-a-milestone)
15. [KPI e Definition of Done](#15-kpi-e-definition-of-done)
16. [Backlog tecnico](#16-backlog-tecnico)
17. [Domande di validazione residue](#17-domande-di-validazione-residue)
18. [Riferimenti](#18-riferimenti)

---

## 1. Visione e obiettivi

### 1.1 Cosa si costruisce

Un **secondo canale** della piattaforma BOT OUTBOUND: campagne di **marketing / re-engagement via WhatsApp** verso contatti **CALDI** — persone che hanno **già scritto al business del cliente** e hanno una chat WhatsApp esistente col suo numero. Killer use case: **riattivare vecchi clienti** (es. Primero: "non compri da 6 mesi, ecco una promo").

Il canale usa la **strada A**: automazione browser **Patchright** su **WhatsApp Web**, collegato come *linked device* al numero WhatsApp Business del cliente. Il cliente **continua a usare l'app normale sul telefono**; il bot invia e legge dallo schermo.

### 1.2 Cosa NON si costruisce

- **Non è cold outbound.** Niente liste comprate/scrapate, niente numeri mai contattati, niente creazione di chat nuove verso sconosciuti. Le chat esistono già.
- **Non è un motore di segmentazione.** Il targeting vive nel CRM del cliente: noi **ingeriamo una lista già filtrata** (CSV; API in fase 2). "Manda *questa* campagna a *questi* numeri."
- **Non è un sistema nuovo.** È un canale su piattaforma esistente: ~50-60% del motore (browser stack, template, timing, worker, caps, AI) si riusa. L'SDD descrive il delta, non l'universo.
- **Non usa API WhatsApp** (né ufficiale né librerie protocollo non ufficiali). Vedi §3 per il razionale.

### 1.3 Obiettivi misurabili del canale (MVP)

| # | Obiettivo | Misura |
|---|---|---|
| O1 | Lanciare una campagna reale end-to-end su un cliente vero (Primero) | Campagna completata, KPI raccolti |
| O2 | Invio affidabile in chat esistenti | ≥95% messaggi confermati inviati (spunta) sul volume di test |
| O3 | Rilevamento risposte via DOM | Risposte rilevate e agganciate al contatto giusto, senza interferire con l'uso umano |
| O4 | Zero ban / zero perdita sessione non recuperabile durante il collaudo | Nessun numero cliente compromesso |
| O5 | Opt-out funzionante | "STOP" rilevato → contatto escluso per sempre, verificato da QA adversarial |

### 1.4 Modello di business (contesto per le scelte tecniche)

- Tommaso vende il servizio a più clienti (**multi-tenant**, ~10 clienti nei primi 6 mesi).
- Ricavo: **costo per messaggio, sotto il prezzo Meta** (~€0,05-0,09/msg marketing IT della Cloud API). Funziona solo se il costo marginale d'invio è ~0 → questo è il motivo per cui la Business API ufficiale è economicamente incompatibile, oltre a prendere possesso del numero.
- Proxy/SIM forniti da Tommaso, costo ribaltato sul cliente.

---

## 2. Glossario

| Termine | Significato |
|---|---|
| **Canale** | Mezzo di outreach: `instagram` (esistente) o `whatsapp` (nuovo). |
| **Tenant** | Cliente di Tommaso (es. Primero). Possiede numeri, contatti, campagne. |
| **Numero WA** | Numero WhatsApp Business del tenant, collegato al bot come linked device. Analogo di `InstagramAccount`. |
| **Contatto WA** | Persona con chat esistente col numero WA del tenant. Identità = telefono E.164. Analogo (lato dati) di `GlobalContact`, ma per-canale. |
| **Sequenza** | Lista ordinata di step (`msg1→msg2→msg3`) con branching base per campagna. |
| **Step** | Un messaggio della sequenza + regola di transizione (attendi X giorni / ha risposto / non ha risposto). |
| **Linked device** | Sessione WhatsApp Web agganciata via QR al telefono del cliente; convive con l'app. |
| **POM** | Page Object Model: classe che incapsula selettori e interazioni con la pagina web (`WhatsAppWebPage`). |
| **HMAC del numero** | `HMAC-SHA256(numero_E164, chiave_segreta)`: pseudonimo deterministico usato come chiave interna al posto del numero in chiaro (P12). |
| **Umano-prima** | Regola di coesistenza MVP: il bot non risponde mai agli inbound; rileva e agisce solo sul flow (branch/stop). |
| **DNC** | Do Not Contact: contatto marcato "non contattare" (opt-out o non-raggiungibile). |

---

## 3. Vincoli e assunzioni

### 3.1 Vincoli (non negoziabili, da decisioni 23/07)

| # | Vincolo | Razionale |
|---|---|---|
| V1 | **Strada A**: browser Patchright su WhatsApp Web. No Business API (C), no librerie protocollo (B: Baileys/wwjs). | C prende possesso del numero (cliente perde l'app) e costa per messaggio (incompatibile col modello ricavo). B = client non ufficiale, quasi certamente intercettato. B riconsiderabile solo se A si rivela insostenibile. |
| V2 | **Solo contatti caldi**: chat esistenti, mai creare conversazioni con numeri nuovi. | Azzera i rischi cold (ban da primo contatto, warm-up aggressivo). È anche il posizionamento commerciale. |
| V3 | **Mono-repo**: codice in `backend/app/` seguendo la struttura esistente; doc in `docs/whatsapp/`. No progetto/DB separato. | Riuso ~50-60%; due codebase anti-detect divergenti = disastro manutentivo. |
| V4 | **Identità per-canale**: un contatto appartiene a UN canale. Nessun merge telefono↔username IG. | Incrociarli è impossibile in modo affidabile e non serve. Semplifica. |
| V5 | **Riuso stack anti-detect esistente** (Patchright, fingerprint, timing lognormale, session manager). Non reinventare con Playwright vanilla. | Collaudato su IG; la detection surface è simile. |
| V6 | **HITL su AI all'inizio**: ogni testo generato da AI passa da approvazione umana prima dell'invio. (MVP: AI nemmeno in perimetro — solo template.) | Rischio reputazionale sul numero del cliente. |
| V7 | **Coesistenza MVP = umano-prima**: il bot non auto-risponde agli inbound. | Le race condition dell'auto-reply sono fase 2 (F4 in sviluppi-futuri). |
| V8 | **Numero in chiaro solo ai confini** (P12): ingest CSV e invio browser. Internamente HMAC + cifratura. | Minimizzazione GDPR; riduce blast radius di leak log/DB. |
| V9 | **1 proxy mobile ↔ max 2 numeri, possibilmente stessa azienda.** Proxy forniti da Tommaso. | Evita correlazione multi-tenant su stesso IP; modello già usato su IG. |
| V10 | **Opt-out per tipo campagna**: marketing → CTA "scrivi STOP" + gestione; follow-up → no. Togglabile, scoped per-canale. | Requisito ePrivacy + igiene anti-segnalazione. |

### 3.2 Assunzioni (da verificare — rimando alle domande §17)

| # | Assunzione | Rischio se falsa | Verifica |
|---|---|---|---|
| A1 | La sessione WhatsApp Web su profilo Chromium persistente sopravvive giorni/settimane senza re-scan QR. | Ops onerosa: il cliente deve riscansionare spesso. | PoC-1 |
| A2 | Si può aprire una chat esistente **per numero** in modo deterministico (search interna o deep-link), senza dipendere dall'ordine della lista chat. | L'invio diventa fragile. | PoC-2 |
| A3 | La lista chat espone abbastanza informazione (titolo, badge unread, preview testo) per rilevare risposte **senza aprire la chat**. | Il reply-watcher marcherebbe "letto" → interferenza con l'umano. | PoC-3 |
| A4 | Il DOM di WhatsApp Web è abbastanza stabile da reggere settimane con selettori robusti. | Manutenzione continua dei selettori. | PoC-3/5 + monitor |
| A5 | Un PC 16-32GB regge ~10 sessioni Chromium persistenti simultanee. | Serve seconda macchina / scaglionamento. | Test infra (M0) |
| A6 | Volumi "qualche centinaio msg/giorno per numero" a contatti caldi non triggherano ban con timing umano. | Ridurre caps, rivedere pacing. | PoC-5 |
| A7 | WhatsApp Business (app) consente il numero di linked device necessario (≥1 slot libero per il bot). | Conflitto col WhatsApp Web che il cliente già usa. | PoC-1 + check per-cliente |
| A8 | Il cliente accetta il modello "sessione scade → riscansiona QR" (a suo carico, guidato). | Serve remote-QR flow più sofisticato. | Contratto/onboarding |

---

## 4. Architettura logica

### 4.1 Principio

Un **canale** = (identità contatto, trasporto invio, rilevamento risposte, regole anti-detect specifiche). Tutto il resto — campagne, template, timing, worker, caps, tenancy, kill-switch, eventi — è **piattaforma condivisa**. Il lavoro vero del cantiere: (1) generalizzare l'identità nel modello dati, (2) scrivere il trasporto WhatsApp (`WhatsAppWebPage` + servizi), (3) aggiungere il layer sequenze (che in prospettiva servirà anche a IG).

### 4.2 Component diagram

```
                 ┌──────────────────────────────────────────────────────┐
                 │                 FRONTEND (Next.js)                   │
                 │   dashboard esistente + sezione WhatsApp:            │
                 │   tenants · numeri WA · campagne WA · sequenze ·     │
                 │   ingest CSV · KPI. (MVP: solo vista admin)          │
                 └───────────────────────┬──────────────────────────────┘
                                         │ REST /api
                 ┌───────────────────────▼──────────────────────────────┐
                 │                 FASTAPI BACKEND                      │
                 │  esistente: campaigns, accounts, admin(kill-switch)  │
                 │  nuovo:  /api/wa/…  (numbers, campaigns, contacts,   │
                 │          ingest, sequences, kpi)  + /api/tenants     │
                 └──────┬──────────────────────────────┬────────────────┘
                        │                              │ enqueue
              ┌─────────▼─────────┐          ┌─────────▼─────────────────┐
              │ Supabase Postgres │          │  Redis + ARQ              │
              │ schema esteso:    │          │  nuovo: wa_send_task      │
              │ tenants, wa_*,    │          │  (fan-out per numero),    │
              │ sequences         │          │  cron: wa_sequence_tick,  │
              └─────────▲─────────┘          │  wa_reply_scan, recovery  │
                        │                    └─────────┬─────────────────┘
                        │                              │
              ┌─────────┴──────────────────────────────▼─────────────────┐
              │              SERVIZI CANALE WHATSAPP (nuovi)             │
              │  wa_ingest (CSV→contatti)   wa_sequence_engine (step/    │
              │  branching/scheduling)      wa_sender (invio 1 msg)      │
              │  wa_reply_watcher (scan lista chat via DOM)              │
              │  wa_optout (STOP→DNC)       pseudonymizer (HMAC/Fernet)  │
              ├──────────────────────────────────────────────────────────┤
              │              CORE CONDIVISO (riuso da IG)                │
              │  template_renderer (A/B/C/D + spintax + placeholder)     │
              │  timing (lognormale) · human_behavior (sessioni/orari)   │
              │  account_manager (warmup/caps/cooldown — generalizzato)  │
              │  context_manager (pool browser, mutex per-profilo)       │
              │  fingerprint · events(Redis) · notifier(Telegram admin)  │
              │  bot_state (kill-switch) · db_resilience · retry         │
              │  ai_personalizer (fase 2, HITL)                          │
              └───────────────────────────┬──────────────────────────────┘
                                          │ Patchright, 1 profilo Chromium
                                          │ persistente per NUMERO WA
              ┌───────────────────────────▼──────────────────────────────┐
              │   Chromium (per numero)  ──►  web.whatsapp.com           │
              │   linked device del numero WhatsApp Business del cliente │
              │   uscita rete: proxy mobile 4G (1 IP ↔ max 2 numeri)     │
              └───────────────────────────┬──────────────────────────────┘
                                          │
                              ┌───────────▼───────────┐
                              │  Telefono del cliente │  ← l'umano continua
                              │  (WhatsApp Business)  │     a usare l'app
                              └───────────────────────┘
```

### 4.3 Confini e interfacce

- **Ingest** (confine dati in ingresso): CSV upload via API. Contratto: colonna `numero` obbligatoria; `nome` + N colonne libere opzionali, usabili come placeholder (`{nome}`, `{ultimo_ordine}`, …). In fase 2 lo stesso contratto viene esposto come endpoint API per CRM (F5) — l'MVP definisce già il contratto, il CSV è solo il primo adattatore.
- **Invio / risposta come interfacce webhook-ready** (decisione 23/07): il sequence engine parla con l'esterno solo tramite eventi interni (`emit_event` su Redis, già esistente): `wa.message.sent`, `wa.reply.received`, `wa.optout`, `wa.step.advanced`, `wa.contact.dnc`. In fase 2 un bridge webhook pubblica questi eventi a n8n e accetta comandi di invio → il flow builder (F1) si innesta senza toccare il motore.
- **Browser** (confine dati in uscita): unico punto dove il numero in chiaro tocca l'esterno (apertura chat + invio). Vedi §12 (P12).
- **Kill-switch**: `bot_state.halted` esistente vale anche per il canale WA (i worker WA fanno lo stesso check interno dei worker IG).

### 4.4 Cosa resta intenzionalmente fuori dall'MVP

Flow builder visuale/n8n (F1), UI cliente self-serve (F2), AI lettura-conversazione (F3), auto-reply con timer (F4), ingest API CRM (F5), analytics avanzate (F6), multi-numero esteso (F7). Dettagli e condizioni di ripresa in `sviluppi-futuri.md`.

---

## 5. Modello dati

### 5.1 Strategia di generalizzazione

Due opzioni valutate:

1. **Generalizzare le tabelle IG esistenti** (`followers` → `contacts` con discriminatore canale): pulita sulla carta, ma `Follower` è profondamente IG-centrico (stati `bio_scraped`, lock scraping, `ig_user_id` BigInt NOT NULL, contatori di fase) e ha migrazioni/query/UI cablate ovunque. Refactoring invasivo ad alto rischio di regressione sul canale IG **in produzione**.
2. **Tabelle nuove per il canale WA + generalizzazione solo dove serve davvero** (campagne, tenancy): il canale IG resta intatto; il canale WA nasce col modello giusto (sequenze, HMAC, tenant) senza ereditare vincoli IG.

**Scelta: opzione 2.** La "generalizzazione dell'identità" promessa nel design si realizza a livello di **piattaforma** (tabella `campaigns` con `channel`, tenancy trasversale, eventi comuni), non forzando i contatti WA dentro tabelle nate per IG. Un'eventuale unificazione `contacts` cross-canale è backlog tecnico (§16), da fare — se mai — quando entrambi i canali sono stabili.

### 5.2 Schema — entità nuove

```
┌────────────┐ 1     N ┌────────────┐ 1     N ┌──────────────────┐
│  tenants   ├─────────┤ wa_numbers │─────────┤  wa_campaigns*   │
└─────┬──────┘         └────────────┘         └───────┬──────────┘
      │ 1                                             │ 1
      │                                               │ N
      │ N                                     ┌───────▼──────────┐
┌─────▼───────┐  N            1               │ wa_sequence_steps│
│ wa_contacts ├───────┐   ┌───────────────────┴──────────────────┘
└─────┬───────┘       │   │
      │           ┌───▼───▼────────────┐ 1      N ┌──────────────┐
      │ N         │ wa_campaign_       ├──────────┤ wa_messages  │
      └──────────►│ contacts (stato    │          └──────────────┘
                  │ per contatto ×     │ 1      N ┌──────────────┐
                  │ campagna)          ├──────────┤ wa_inbound_  │
                  └────────────────────┘          │ events       │
                                                  └──────────────┘
  (*) wa_campaigns = riga in `campaigns` con channel='whatsapp',
      oppure tabella dedicata — vedi decisione D2 sotto.
```

#### `tenants` (nuova — piattaforma, non solo WA)

| Colonna | Tipo | Note |
|---|---|---|
| `id` | PK | |
| `name` | str | es. "Primero" |
| `status` | enum | `active \| suspended` |
| `settings` | JSON | tier managed/self-serve (fase 2), default caps, ecc. |
| `created_at` | ts | |

MVP: solo vista admin → il tenant è un'etichetta di scoping dati, non un login. Le tabelle WA hanno tutte `tenant_id` NOT NULL. Il canale IG esistente resta senza tenant (mono-tenant legacy); l'aggancio di IG alla tenancy è backlog (§16).

#### `wa_numbers` (nuova — analogo di `instagram_accounts`)

| Colonna | Tipo | Note |
|---|---|---|
| `id` | PK | |
| `tenant_id` | FK | |
| `label` | str | nome leggibile ("Primero — sede") |
| `phone_hmac` | str, UNIQUE | HMAC del numero del cliente (chiave interna) |
| `encrypted_phone` | str | Fernet — serve solo per display admin mascherato e diagnostica |
| `status` | enum | vedi state machine §8.3: `pending_qr \| active \| qr_required \| disconnected \| cooldown \| suspended \| retired` |
| `browser_profile` | str | path profilo Chromium persistente (convenzione `data/browser_profiles/wa_<id>`) |
| `proxy_url` | str | proxy mobile assegnato (vincolo V9 applicato a livello applicativo: max 2 numeri per proxy, stesso tenant) |
| `daily_cap` | int | cap messaggi/giorno per numero (default basso, modificabile a mano) |
| `warmup_day` | int | riuso semantica IG: rampa graduale del cap |
| `sent_today` / `sent_date` | int / str | contatore date-aware (stesso pattern lazy-reset di `scrape_lookups_date`, migrazione 018) |
| `session_checked_at` | ts | ultimo health-check sessione |
| `notes` | text | |

Niente password: l'autenticazione è la sessione QR nel profilo browser. Il "login" è lo scan QR fatto dal cliente (flusso §7.6).

#### `wa_contacts` (nuova — anagrafica per-tenant, cross-campagna)

| Colonna | Tipo | Note |
|---|---|---|
| `id` | PK | |
| `tenant_id` | FK | |
| `phone_hmac` | str | UNIQUE per tenant (`UNIQUE(tenant_id, phone_hmac)`) |
| `encrypted_phone` | str | Fernet — decifrato SOLO da wa_sender al momento dell'apertura chat |
| `display_name` | str nullable | da CSV (`nome`) |
| `chat_title` | str nullable | titolo della chat come appare su WhatsApp Web (appreso al primo invio — serve al reply-watcher per il matching, vedi §7.3) |
| `attributes` | JSON | colonne libere del CSV (`{ultimo_ordine: "2026-01-10", …}`) — placeholder per i template |
| `opted_out` | bool | STOP ricevuto → mai più contattabile (per-canale, per-tenant) |
| `opted_out_at` | ts nullable | |
| `do_not_contact` | bool | non-raggiungibile / fallimenti permanenti (decisione 4.4) |
| `dnc_reason` | str nullable | `optout \| unreachable \| invalid_number \| manual` |
| `first_seen_at` / `last_contacted_at` / `last_replied_at` | ts | KPI + frequency cap cross-campagna |

Dedup cross-campagna: analogo di `global_contacts`, ma **per-tenant e per-canale** (V4). Il frequency cap "non ricontattare chi ha ricevuto marketing da < X giorni" (P1) si applica qui, cross-campagna.

#### `wa_campaigns` — decisione D2

Due opzioni:

- **D2a** — riga in `campaigns` esistente con colonna nuova `channel` (`'instagram'` default retro-compatibile, `'whatsapp'`), `tenant_id` nullable, campi IG nullable per WA. Pro: dashboard/controlli/eventi unificati subito. Contro: `campaigns` ha già ~40 colonne IG-specifiche e una state machine IG cablata in API/frontend; ogni endpoint esistente va guardato per il caso `channel='whatsapp'`.
- **D2b** — tabella `wa_campaigns` dedicata + colonna `channel` aggiunta a `campaigns` in un secondo momento, quando si unifica la UI. Pro: zero rischio sul canale IG in produzione; state machine WA pulita (§8.1). Contro: due liste campagne in dashboard finché non si unifica.

**Raccomandazione: D2b per l'MVP** (coerente con la scelta 5.1: non toccare ciò che è in produzione), con la UI che presenta le due liste sotto un'unica vista "Campagne" filtrabile per canale. Unificazione fisica = backlog (§16). **Da confermare con Tommaso** (domanda Q25).

| Colonna (`wa_campaigns`) | Tipo | Note |
|---|---|---|
| `id` | PK | |
| `tenant_id` / `wa_number_id` | FK | un numero dedicato per campagna (decisione 23/07) |
| `name` | str | |
| `campaign_type` | enum | `marketing \| followup` — pilota l'obbligo opt-out (V10) |
| `status` | enum | §8.1 |
| `daily_limit` | int | cap per-campagna (in AND col cap per-numero) |
| `optout_enabled` | bool | default: True se marketing |
| `optout_cta` | str | testo CTA appeso al primo messaggio (es. "Scrivi STOP per non ricevere più messaggi") |
| `active_hours_start/end` | str | finestra oraria (riuso semantica IG) |
| `session_min/max_messages`, `break_min/max_minutes` | int | pacing sessioni (riuso `human_behavior`) |
| contatori denormalizzati | int | `total_contacts, sent, replied, opted_out, failed` |
| `created_at` / `started_at` / `completed_at` | ts | |

#### `wa_sequence_steps` (nuova — la sequenza della campagna)

| Colonna | Tipo | Note |
|---|---|---|
| `id` | PK | |
| `campaign_id` | FK | |
| `step_index` | int | 0-based, `UNIQUE(campaign_id, step_index)` |
| `template_a/b/c/d` | text | A obbligatorio, B/C/D opzionali — stesso formato IG (spintax + placeholder); render via `template_renderer.pick_template()`/`render_template()` |
| `send_condition` | enum | `always` (step 0) \| `if_no_reply` \| `if_replied` |
| `wait_days` | int | attesa dallo step precedente prima di valutare la condizione |

MVP: set minimo di operatori = esattamente questi tre (`always`, `if_no_reply`, `if_replied`) + `wait_days`. Niente rami annidati, niente condizioni su contenuto risposta (fuori: F1/F3). La struttura a step lineari con condizione è volutamente un sottoinsieme serializzabile di un flow n8n → migrabile a F1 senza conversione dati distruttiva.

#### `wa_campaign_contacts` (nuova — stato per contatto × campagna)

| Colonna | Tipo | Note |
|---|---|---|
| `id` | PK | |
| `campaign_id` / `contact_id` | FK | `UNIQUE(campaign_id, contact_id)` |
| `status` | enum | §8.2: `queued \| in_sequence \| replied \| completed \| opted_out \| failed \| skipped` |
| `current_step` | int | ultimo step inviato (-1 = nessuno) |
| `next_action_at` | ts nullable | quando il sequence engine deve rivalutare questo contatto (indice: è la colonna su cui gira il tick) |
| `replied_at_step` | int nullable | a che step ha risposto (KPI drop-off, F6) |
| `locked_by` / `locked_at` | str/ts | claim atomico per worker (stesso pattern `locked_by_account_id` di `followers`, timeout stale identico) |
| `failure_count` / `last_error` | int/str | |

#### `wa_messages` (nuova — log invii, analogo di `messages`)

| Colonna | Tipo | Note |
|---|---|---|
| `id` | PK | |
| `campaign_id` / `contact_id` / `wa_number_id` | FK | |
| `step_index` | int | |
| `template_variant` | char | 'a'..'d' (A/B testing come IG) |
| `rendered_text` | text | testo effettivamente inviato (serve per audit + retry; contiene PII del *contenuto*, non il numero — vedi §12) |
| `status` | enum | `queued \| sending \| sent \| failed \| skipped` |
| `error` | str nullable | |
| `queued_at` / `sent_at` | ts | |
| `delivery_check` | enum nullable | `none \| clock \| single_tick \| double_tick` — cosa ha visto il POM dopo l'invio (best-effort, PoC-2 dice quanto è affidabile) |

#### `wa_inbound_events` (nuova — risposte rilevate)

| Colonna | Tipo | Note |
|---|---|---|
| `id` | PK | |
| `tenant_id` / `wa_number_id` | FK | |
| `contact_id` | FK nullable | NULL se il matching fallisce (chat non riconducibile a un contatto nostro → ignorata ai fini del flow, tenuta per diagnostica) |
| `detected_at` | ts | |
| `preview_text` | str nullable | SOLO la preview dalla lista chat (troncata da WhatsApp), usata per opt-out detection; **non** si salva la conversazione (minimizzazione, §12) |
| `matched_by` | enum | `chat_title \| phone \| none` |
| `processed` | bool | consumato dal sequence engine |

**Nota di minimizzazione:** in MVP non si apre la chat per leggere le risposte e non si archivia il testo completo — basta *che* abbia risposto (branching) + la preview (opt-out "STOP"). La lettura conversazione completa è F3 (fase 2), con le sue implicazioni GDPR.

### 5.3 Cosa NON cambia

`instagram_accounts`, `campaigns` (salvo eventuale colonna `channel` futura), `followers`, `messages`, `global_contacts`, tutto il dominio lead qualification: **intoccati**. Il canale IG in produzione non deve accorgersi di questo cantiere fino all'unificazione UI.

### 5.4 Migrazioni

Nuova catena Alembic (025+…): `tenants`, `wa_numbers`, `wa_contacts`, `wa_campaigns`, `wa_sequence_steps`, `wa_campaign_contacts`, `wa_messages`, `wa_inbound_events`. Tutte additive — zero `ALTER` su tabelle esistenti in MVP (conseguenza diretta di D2b). Regole note del repo: migrare PRIMA di far girare codice che dichiara colonne nuove; attenzione ai lock `idle in transaction` su Supabase.

---

## 6. Componenti: riuso vs adattamento vs nuovo

Tabella operativa (raffina quella del living doc con i moduli target):

### 6.1 RIUSO as-is (import diretto, zero modifiche)

| Componente | Path | Uso nel canale WA |
|---|---|---|
| Template A/B/C/D + spintax + placeholder | `services/template_renderer.py` | Render step sequenza; i placeholder pescano da `wa_contacts.attributes` invece che dalla bio IG |
| Pool browser + profili persistenti + mutex per-profilo | `browser/context_manager.py` | 1 profilo per `wa_number`; stesso mutex "1 browser alla volta per profilo" |
| Fingerprint deterministico | `browser/fingerprint.py` | seed = wa_number id |
| Timing lognormale | `utils/timing.py` | delay tra messaggi, typing, pause |
| Sessioni/finestre orarie/break | `services/human_behavior.py` | pacing invii per numero |
| Retry + backoff | `utils/retry.py` | |
| Classificazione errori DB transitori | `utils/db_resilience.py` | stessi `Retry(defer)` sui blip Supabase |
| Kill-switch globale | `models/bot_state.py`, `services/bot_state_service.py` | check interno nei worker WA |
| Eventi live-log | `utils/events.py` | namespace eventi `wa.*` |
| Notifiche Telegram admin | `services/notifier.py` | alert operativi a Tommaso (QR scaduto, selettori rotti, stop campagna). NB: notifiche *admin*, non notifiche-risposte al cliente (escluse da decisione) |
| Crypto Fernet | `utils/crypto.py` | `encrypted_phone` |
| Scheletro worker ARQ (fan-out, `_job_id` dedup, `Retry(defer)`, stagger, lease) | `workers/task_queue.py`, `services/work_enqueue.py`, `services/account_lease.py` | pattern identico, task nuovi |
| AI multi-provider + failover | `services/ai_personalizer.py`, `adapters/ai.py` | fase 2 (F3), con HITL e vincoli GDPR |

### 6.2 DA ADATTARE (generalizzazione o estrazione)

| Componente | Path | Adattamento |
|---|---|---|
| Umanizzazione input (typing lognormale + Shift+Enter, typo, click, browse) | oggi primitive dentro `browser/instagram_page.py` | **Estrarre** in `browser/human_input.py` condiviso; `instagram_page` e `whatsapp_page` lo importano. Refactor a basso rischio (spostamento di funzioni), va fatto per non duplicare |
| Caps/warmup/cooldown | `services/account_manager.py` | Il concetto (rampa, contatore date-aware, cooldown) si riusa; l'implementazione è cablata su `InstagramAccount` → servizio `wa_number_manager.py` che replica il pattern su `wa_numbers` (non generalizzare l'esistente in MVP) |
| Campaign engine (loop, claim atomico, limiti live) | `services/campaign_orchestrator.py` | NON si adatta il file: si scrive `wa_sequence_engine.py` nuovo **copiando i pattern** (claim `UPDATE WHERE locked_by IS NULL`, daily limit con query live, batch short-lived). L'orchestratore IG resta com'è |
| Estrazione/normalizzazione contatti | `utils/contact_extract.py` | Riuso della normalizzazione telefono → E.164 nel parser CSV (`wa_ingest`) |
| Reply handling (pattern) | `services/reply_checker.py` | Pattern "scansione periodica + match + update stato" sì; transport instagrapi no → `wa_reply_watcher.py` su DOM |
| Controlli campagna condivisi (web+Telegram) | `services/campaign_control.py` | Aggiungere i comandi pause/resume per campagne WA (stesso pre-check Redis) |

### 6.3 NUOVO (da scrivere)

| Componente | Path proposto | Contenuto | Blueprint |
|---|---|---|---|
| POM WhatsApp Web | `browser/whatsapp_page.py` | Selettori + interazioni: stato sessione (QR? logged?), apertura chat per numero, invio con typing umano, lettura lista chat (titoli/badge/preview), screenshot diagnostici | `browser/instagram_page.py` (stile: selettori scoped, dismiss popup, no assumzioni layout) |
| Worker invio | `workers/wa_worker.py` + `services/wa_sender.py` | Mini-sessioni per-numero: claim batch, browser, invio, `Retry(defer)` a fine sessione/budget | **`services/browser_bio.py`** (mini-sessione browser per-account + claim atomico + defer + escalation soft-block) — è il calco dichiarato |
| Sequence engine | `services/wa_sequence_engine.py` | Valuta `next_action_at`, applica `send_condition`, accoda invii, avanza step, chiude contatti | pattern orchestratore IG |
| Reply watcher | `services/wa_reply_watcher.py` + task cron | Scan periodico lista chat per numero attivo, produce `wa_inbound_events`, match contatto (§7.3) | `reply_checker.py` (solo pattern) |
| Opt-out | dentro `wa_reply_watcher`/`wa_sequence_engine` | regex STOP-like su `preview_text` → `opted_out` + stop sequenza + evento | P2 |
| Ingest CSV | `services/wa_ingest.py` + endpoint | parse, validazione E.164, dedup, HMAC, staging→contatti, report righe scartate | `import_resolver.py` / `ig_username.py` (parser difensivo) |
| Pseudonymizer | `utils/phone_pseudonym.py` | `hmac_phone(e164) -> str` + helper masking log (`+39•••••077`) | P12 |
| Session/QR flow | `services/wa_session.py` + endpoint admin | health-check sessione, stato `qr_required`, pagina admin che mostra il QR al cliente (screenshot/stream), conferma link | `manual_login.py` (login browser assistito IG) |
| API REST | `api/wa_campaigns.py`, `api/wa_numbers.py`, `api/wa_contacts.py`, `api/tenants.py` | CRUD + start/pause/stop + ingest + KPI | `api/campaigns.py` |
| Frontend | `frontend/src/app/wa/…` | pagine campagne WA, numeri, ingest, KPI | pagine campaigns esistenti |

---

## 7. Flussi principali (sequence)

### 7.1 Ingest CSV

```
Admin (Tommaso)      API /wa/ingest        wa_ingest             DB
     │  upload CSV        │                    │                  │
     ├────────────────────►                    │                  │
     │                    ├─ parse righe ──────►                  │
     │                    │                    ├─ normalizza E.164│
     │                    │                    │  (contact_extract)
     │                    │                    ├─ hmac_phone()    │
     │                    │                    ├─ upsert wa_contacts
     │                    │                    │  (skip: opted_out│
     │                    │                    │   / do_not_contact)
     │                    │                    ├─ crea wa_campaign_
     │                    │                    │  contacts(queued) │
     │                    │                    ├──────────────────►
     │  report: N ok,     │                    │                  │
     │  M scartate (righe │◄─ righe invalide,  │                  │
     │  + motivo), K già  │   duplicati, DNC   │                  │
     │  DNC/opted-out     │                    │                  │
     ◄────────────────────┤                    │                  │
```

Regole: numero invalido → riga scartata con motivo (mai "aggiustata" silenziosamente); contatto `opted_out`/`do_not_contact` → **escluso e riportato** (mai re-incluso da un nuovo CSV — l'opt-out vince sull'ingest); duplicato nel file → dedup. Il numero in chiaro vive solo nella richiesta e nella colonna `encrypted_phone`; il report all'admin usa forma mascherata.

### 7.2 Invio step campagna (happy path)

```
cron/API      wa_sequence_engine     ARQ        wa_sender      WhatsAppWebPage    WhatsApp Web
   │ start / tick     │               │             │                │                │
   ├──────────────────►               │             │                │                │
   │                  ├─ SELECT wa_campaign_contacts                 │                │
   │                  │  WHERE next_action_at<=now                   │                │
   │                  │  AND status IN (queued,in_sequence)          │                │
   │                  │  + claim atomico (locked_by)                 │                │
   │                  ├─ valuta send_condition dello step successivo │                │
   │                  │  (if_no_reply / if_replied via wa_inbound)   │                │
   │                  ├─ render template (pick A-D + spintax +       │                │
   │                  │  placeholder da attributes)                  │                │
   │                  ├─ crea wa_messages(queued)                    │                │
   │                  ├─ enqueue wa_send_task(number) ──►            │                │
   │                  │               ├─ job dedup _job_id=wa:{campaign}:{number}     │
   │                  │               ├─────────────►                │                │
   │                  │               │  check: kill-switch, finestra oraria,         │
   │                  │               │  cap numero (date-aware), cap campagna live   │
   │                  │               │             ├─ mutex profilo─►                │
   │                  │               │             │                ├─ assert sessione ok
   │                  │               │             │                ├─ apri chat per │
   │                  │               │             │                │  numero (A2)   ├─►
   │                  │               │             │                ├─ verifica chat │
   │                  │               │             │                │  ESISTENTE (V2:│
   │                  │               │             │                │  se nessuna    │
   │                  │               │             │                │  cronologia →  │
   │                  │               │             │                │  SKIP, no invio)│
   │                  │               │             │                ├─ salva chat_title
   │                  │               │             │                ├─ typing umano  │
   │                  │               │             │                │  (human_input, │
   │                  │               │             │                │  Shift+Enter)  ├─►
   │                  │               │             │                ├─ Enter → check │
   │                  │               │             │                │  spunta (best  │
   │                  │               │             │                │  effort)       │
   │                  │               │             ├─ wa_messages(sent), current_step++,
   │                  │               │             │  next_action_at = now + wait_days
   │                  │               │             │  del prossimo step; last_contacted_at
   │                  │               │             ├─ emit wa.message.sent
   │                  │               │             ├─ delay lognormale → prossimo contatto
   │                  │               │             │  del batch; a fine sessione/cap:
   │                  │               │             │  Retry(defer=break)  [pattern browser_bio]
```

Punti fermi:
- **Guardia "chat esistente" (V2):** se all'apertura la chat non ha cronologia (contatto mai sentito), il contatto va in `skipped` con motivo `no_existing_chat` — il canale non crea conversazioni nuove. Questo è un check bloccante nel POM, non una convenzione.
- **Cap in AND**: cap numero (warmup/daily) ∧ cap campagna ∧ finestra oraria ∧ kill-switch. Tutti con query live, non contatori stale (lezione IG).
- **Worker short-lived**: mai sleep lunghi in-job; sessione finita → `Retry(defer)`; micro-yield se la sessione è lunga (lezione `job_timeout` della Fase Bio).
- **Opt-out CTA**: se `optout_enabled`, la CTA è appesa al **primo** messaggio della sequenza (step 0), non a tutti.

### 7.3 Rilevamento risposta via DOM (senza aprire la chat)

Il vincolo chiave (coesistenza, §9): **il reply-watcher non apre mai le chat** — aprirle marcherebbe "letto" e brucerebbe le notifiche del cliente. Si legge solo la **lista chat** (pannello sinistro): titolo, badge non-letti, preview ultimo messaggio, timestamp.

```
cron wa_reply_scan     wa_reply_watcher       WhatsAppWebPage         DB
      │ ogni N min           │                      │                  │
      ├─────────────────────►│                      │                  │
      │   (solo numeri con   ├─ mutex profilo ─────►│                  │
      │   campagne attive;   │                      ├─ scroll lista    │
      │   stessa finestra    │                      │  chat (solo      │
      │   oraria degli invii)│                      │  pannello sx),   │
      │                      │                      │  raccogli righe: │
      │                      │                      │  {title, unread, │
      │                      │                      │   preview, ts}   │
      │                      │◄─────────────────────┤                  │
      │                      ├─ per ogni riga con unread>0:            │
      │                      │   match contatto:                       │
      │                      │   1) title == wa_contacts.chat_title    │
      │                      │      (appreso al primo invio)           │
      │                      │   2) title parsabile come numero →      │
      │                      │      hmac → wa_contacts.phone_hmac      │
      │                      │   3) nessun match → evento non          │
      │                      │      associato (diagnostica), skip      │
      │                      ├─ dedup vs ultimo evento (stesso title + │
      │                      │   ts/preview già visti → skip)          │
      │                      ├─ INSERT wa_inbound_events ─────────────►│
      │                      ├─ preview ~ /\b(stop|basta|cancellami|   │
      │                      │   non scrivermi|unsubscribe)\b/i →      │
      │                      │   opted_out=True + stop sequenza +      │
      │                      │   emit wa.optout                        │
      │                      ├─ altrimenti: wa_campaign_contacts →     │
      │                      │   replied (se in_sequence), replied_at, │
      │                      │   emit wa.reply.received                │
```

Limiti accettati in MVP (espliciti):
- La preview è troncata e mostra solo l'**ultimo** messaggio: se il contatto scrive 3 messaggi, vediamo l'ultimo. Per branching sì/no e opt-out basta. (Se "STOP" è seguito da altro messaggio prima dello scan → possibile miss: mitigazione = scan frequente + fallback umano; domanda Q58.)
- Se l'**umano legge la chat dal telefono prima dello scan**, il badge unread sparisce → la risposta può sfuggire al watcher. Mitigazioni possibili (da PoC-3): confronto `preview/ts` dell'ultima riga anche senza badge, direzione del messaggio se il DOM la espone. Rischio residuo documentato; il branching `if_no_reply` degrada in modo conservativo (manda un follow-up a chi aveva risposto = fastidio, non disastro; frequency cap lo limita).
- Risposte arrivate **fuori finestra di scan** vengono viste allo scan successivo: il branching è per definizione asincrono (granularità `wait_days`, non minuti) → nessun requisito realtime.

### 7.4 Branching sequenza (tick)

```
        wa_sequence_tick (cron, ogni ~15 min, dentro finestra oraria)
                              │
        per ogni wa_campaign_contacts con next_action_at <= now:
                              │
              ┌───────────────┴────────────────┐
              │ step successivo esiste?        │
              └───────┬────────────────┬───────┘
                     no               sì
                      │                │
               status=completed   send_condition?
                                       │
                 ┌──────────────┬──────┴───────┬───────────────┐
                 │ always       │ if_no_reply  │ if_replied    │
                 │ → accoda     │ replied?     │ replied?      │
                 │   invio      │  no → accoda │  sì → accoda  │
                 │              │  sì → status │  no → status  │
                 │              │   =replied,  │   =completed  │
                 │              │   stop seq.  │   (ramo morto)│
                 └──────────────┴──────────────┴───────────────┘

        regole trasversali (vincono su tutto, in quest'ordine):
        opted_out / do_not_contact → stop immediato, status=opted_out/skipped
        kill-switch / campagna paused → il tick non tocca nulla
        frequency cap tenant (last_contacted_at troppo recente da ALTRA
        campagna) → rinvia (next_action_at += 1 giorno), non salta
```

Nota semantica (da confermare, Q29): default MVP = **una risposta qualsiasi ferma la sequenza** (`replied` è terminale salvo step espliciti `if_replied`). È il comportamento sicuro: chi risponde entra in conversazione umana (umano-prima), il marketing automatico si toglie di mezzo.

### 7.5 Opt-out (dettaglio)

1. Watcher rileva preview matching STOP-regex (case-insensitive, parole intere, lista parole configurabile per tenant — italiano di default).
2. `wa_contacts.opted_out=True`, `dnc_reason='optout'`, timestamp.
3. Tutte le `wa_campaign_contacts` attive del contatto (qualsiasi campagna del tenant) → `opted_out`, sequenze fermate, invii `queued` cancellati.
4. Evento `wa.optout` + contatore campagna.
5. Ingest futuri: il contatto è escluso e riportato (7.1). Nessuna riattivazione automatica; riattivazione solo manuale admin con motivazione (caso: falso positivo).
6. Falso positivo ("non mi stoppare ahah"): accettato in MVP — meglio un opt-out di troppo che uno mancato. L'admin può riattivare a mano.

### 7.6 Sessione / QR (onboarding numero e recovery)

```
Admin            API /wa/numbers        wa_session         WhatsAppWebPage      Cliente
  │ crea numero        │                    │                    │                │
  ├────────────────────►                    │                    │                │
  │ "avvia link"       ├───────────────────►│                    │                │
  │                    │                    ├─ apri profilo ────►│                │
  │                    │                    │  Chromium nuovo    ├─ web.whatsapp  │
  │                    │                    │                    │  mostra QR     │
  │                    │                    │◄─ screenshot QR ───┤                │
  │◄─ QR (pagina admin,│◄───────────────────┤                    │                │
  │   auto-refresh) ───┤                    │                    │                │
  │ mostra/manda QR al cliente ─────────────────────────────────────────────────► │
  │                    │                    │                    │   scan QR      │
  │                    │                    │◄─ sessione attiva ─┤◄───────────────┤
  │                    │                    ├─ status=active     │                │
  │                    │                    ├─ health-check periodico (cron):     │
  │                    │                    │  sessione caduta → status=qr_required
  │                    │                    │  + alert Telegram admin + pausa     │
  │                    │                    │  campagne del numero                │
```

Decisione 23/07: re-scan a carico del cliente, guidato. Il flusso QR remoto (screenshot del QR servito su pagina admin, girato al cliente via canale sicuro) è il meccanismo; i dettagli UX (refresh QR ogni ~30s, scadenza) sono da PoC-1. Il cliente non tocca mai il PC di Tommaso.

---

## 8. State machine

### 8.1 Campagna WA

```
                       ┌────────┐
        crea           │ draft  │  (ingest CSV, definisci sequenza, numero)
                       └───┬────┘
                     start │  (valida: numero active, ≥1 step, ≥1 contatto)
                           ▼
                       ┌────────┐   pausa manuale / numero qr_required /
                       │running │──────────────────────────────┐
                       └───┬────┘                              ▼
                           │        ▲                     ┌────────┐
                           │        └──── resume ─────────┤ paused │
                           │                              └───┬────┘
        tutti i contatti   │                                  │ stop definitivo
        in stato terminale ▼                                  ▼
                       ┌──────────┐                      ┌─────────┐
                       │completed │                      │ stopped │
                       └──────────┘                      └─────────┘
                           errore bloccante (sessione irrecuperabile,
                           selettori rotti persistenti) → ┌───────┐
                                                          │ error │ (ripartibile
                                                          └───────┘  dopo fix)
```

Differenza voluta vs IG: **niente stati di scraping** (non c'è scraping: la lista arriva ingerita in `draft`). La macchina è più semplice; `running` copre l'intero ciclo sequenze.

### 8.2 Contatto × campagna (`wa_campaign_contacts.status`)

```
   ingest        ┌────────┐  primo invio ok   ┌─────────────┐
  ──────────────►│ queued ├──────────────────►│ in_sequence │◄──┐
                 └──┬─────┘                   └──┬───┬───┬───┘  │ step
                    │                            │   │   │      │ successivo
   già DNC/optout   │                            │   │   └──────┘ inviato
   o no_existing_   │            risposta rilevata   │
   chat o numero    │                            │   │ sequenza esaurita
   invalido         ▼                            ▼   ▼
                 ┌─────────┐              ┌─────────┐ ┌───────────┐
                 │ skipped │              │ replied │ │ completed │
                 └─────────┘              └─────────┘ └───────────┘
                    ▲                            │
   fallimenti       │             "STOP" rilevato▼
   permanenti       │                     ┌───────────┐
   (unreachable) ───┘                     │ opted_out │
   + wa_contacts.do_not_contact=True      └───────────┘
                 ┌────────┐
   errori invio  │ failed │ → retry con backoff; oltre soglia →
   transitori    └────────┘   do_not_contact('unreachable') + skipped
```

Stati terminali: `replied`, `completed`, `opted_out`, `skipped`. `failed` è transitorio (retry) finché non supera la soglia → catalogazione "non contattare" (decisione 4.4) + eventuale report al cliente.

### 8.3 Numero WA (`wa_numbers.status`)

```
  ┌────────────┐  QR scansionato  ┌────────┐  health-check fallito / logout
  │ pending_qr ├─────────────────►│ active ├────────────────────────────┐
  └────────────┘                  └─┬──┬───┘                            ▼
        ▲                           │  │ segnali rischio          ┌─────────────┐
        │ re-link (nuovo QR)        │  │ (warning WhatsApp,       │ qr_required │
        └───────────────────────────┼──┤  fallimenti anomali)     └──────┬──────┘
                                    │  ▼                                 │ sessione
                                    │ ┌──────────┐  timer/manuale        │ persa del
                                    │ │ cooldown ├───► active            │ tutto
                                    │ └──────────┘                       ▼
                     dismissione    │                              ┌──────────────┐
                     manuale        ▼                              │ disconnected │
                              ┌──────────┐                         └──────────────┘
                              │ retired  │   ban/blocco numero → ┌───────────┐
                              └──────────┘   (evento grave)      │ suspended │
                                                                 └───────────┘
```

`qr_required`/`disconnected`/`suspended` mettono **in pausa automatica** le campagne del numero + alert Telegram admin. `cooldown` = pausa precauzionale (riuso semantica IG) senza perdere la sessione.

---

## 9. Coesistenza umano ↔ bot

Il numero è del cliente e il cliente lo usa **contemporaneamente** dal telefono. Regole MVP (umano-prima, V7):

| # | Regola | Implementazione |
|---|---|---|
| C1 | Il bot **non risponde mai** agli inbound. | Nessun path di codice invia fuori da una sequenza di campagna. |
| C2 | Il bot **non apre le chat per leggere**. | Reply-watcher legge solo la lista chat (§7.3). Aprire una chat = solo per inviare uno step. |
| C3 | Effetto collaterale accettato: quando il bot apre una chat per **inviare**, gli unread di quella chat risultano letti. | Mitigazione: si apre solo al momento dell'invio; se la chat ha badge unread al momento dell'apertura → registra prima l'inbound event (la risposta non va persa) e **rivaluta la condizione dello step prima di inviare** (se lo step era `if_no_reply` e c'è una risposta appena scoperta → non inviare, marca `replied`). |
| C4 | Se l'**ultimo messaggio in chat è dell'umano-business** (il cliente sta conversando a mano), il bot **non si intromette**: step rinviato. | Check nel POM prima del typing: direzione ultimo messaggio + età; se outbound umano recente (< X ore, configurabile) → rinvia `next_action_at`. (Distinguere "outbound del bot" da "outbound dell'umano": il bot conosce i propri `wa_messages.sent_at` — un outbound che il bot non ha inviato è dell'umano.) |
| C5 | Invii solo in finestra oraria del tenant; niente invii notturni "da bot". | `human_behavior` + config campagna. |
| C6 | Il cliente vede le risposte dalle **notifiche native** di WhatsApp Business e gestisce in autonomia. Il bot non notifica le risposte (né Telegram né altro). | Decisione 23/07. Il bot notifica solo eventi *operativi* all'admin (QR, errori). |

Fase 2 (F4): auto-reply con timer anti-doppio-messaggio + lock (numero, contatto) + check "ultimo outbound umano" — già predisposto dal check C4.

---

## 10. Threat model e anti-ban

### 10.1 Premessa di frame

I contatti sono caldi: chat esistenti, relazione reale col business. I vettori di ban classici del cold outbound (contatti mai visti, chat nuove in massa, numero appena creato) **non esistono qui**. Il rischio residuo è concentrato in: (T1) fatigue/segnalazioni da marketing ripetuto, (T2) detection dell'automazione lato client, (T3) errori operativi nostri.

### 10.2 Tabella minacce → mitigazioni

| # | Minaccia | Vettore | Mitigazioni | Residuo |
|---|---|---|---|---|
| T1 | **Segnalazioni/blocchi utente** ("marketing fatigue") → WhatsApp banna il numero su segnali spam aggregati | Promo ripetute, troppo frequenti, impersonali | Frequency cap cross-campagna per contatto; cap giornaliero basso per numero (warmup); spintax A/B/C/D (testi mai identici); placeholder personalizzazione; opt-out CTA visibile (chi può uscire non segnala); qualità contenuto = responsabilità condivisa col cliente (da contratto) | Medio-basso. KPI opt-out/blocchi monitorati per campagna; soglia di allarme → pausa |
| T2 | **Detection automazione client-side** (WhatsApp Web rileva pattern non umani) | Typing/click meccanici, ritmi costanti, sessione browser anomala | Patchright (no webdriver flags) + fingerprint per-profilo + profilo Chromium persistente (mai incognito); typing lognormale con typo e pause (human_input); delay lognormali tra invii; sessioni corte con break (human_behavior); finestra oraria; scroll/idle naturali tra le azioni | Basso-medio. PoC-5 misura; nessuna garanzia formale |
| T3 | **Correlazione multi-numero** (stesso IP/fingerprint per tenant diversi) | Datacenter IP, numeri N su 1 IP | Proxy mobili 4G (tether USB + EveryProxy); **1 IP ↔ max 2 numeri, stessa azienda** (V9); fingerprint browser distinto per numero | Basso |
| T4 | **Linked-device hygiene** (sessione bot vista come dispositivo sospetto) | Login/logout frequenti, IP ballerino sulla stessa sessione | Sessione stabile su profilo persistente; proxy fisso per numero; niente re-login se non necessario | Basso; PoC-1 osserva |
| T5 | **Volume burst** | Coda che scarica 200 msg appena parte la finestra oraria | Pacing: delay lognormale per-messaggio + cap sessione + stagger tra numeri (riuso stagger IG) | Basso |
| T6 | **Contenuto vietato** (categorie policy WhatsApp) | Il cliente carica una promo che viola le policy commerce | Responsabilità cliente da contratto; review campagna lato admin prima dello start (MVP: Tommaso vede tutto comunque) | Contrattuale |
| T7 | **Leak PII** (numeri in log/DB/backup) | logging ingenuo, dump, errori | P12: HMAC come chiave interna, Fernet per il numero, masking nei log (`+39•••••077`), niente testo conversazioni salvato (§5.2, §12) | Basso |
| T8 | **Accesso non autorizzato alla dashboard** | Dashboard esposta, multi-tenant | Auth esistente (`api/auth.py`, `utils/auth_deps.py`); MVP solo admin; scoping tenant su ogni query WA fin dal giorno 1 (anche se l'unico utente è Tommaso) | Basso |
| T9 | **Il PC di casa è un single point of failure + contiene sessioni di numeri altrui** | Furto/compromissione macchina | Disco/profili su macchina dedicata; secrets in `.env` fuori repo; valutare cifratura disco (Q99) | Da decidere |

### 10.3 Parametri anti-ban iniziali (proposta, da tarare in PoC-5)

| Parametro | Valore iniziale | Note |
|---|---|---|
| Cap numero, giorno 1-3 (warmup) | 20-30 msg/giorno | anche se il numero è "vecchio": il *comportamento bot* è nuovo |
| Cap numero, regime | 100-200 msg/giorno | "qualche centinaio" è il tetto dichiarato; salire gradualmente |
| Delay tra messaggi | lognormale, mediana ~90s, sigma 0.7 | stesso generatore IG |
| Sessione | 8-15 msg, poi break 20-40 min | riuso human_behavior |
| Finestra oraria | 9:30-19:30 (per tenant) | orario da business, non da bot |
| Frequency cap contatto | ≥ 14 giorni tra due campagne marketing | cross-campagna, per tenant |
| Soglia allarme opt-out | > 5% opt-out su una campagna → pausa + review | KPI §15 |

---

## 11. Failure mode analysis

| # | Guasto | Rilevamento | Risposta automatica | Recovery |
|---|---|---|---|---|
| FM1 | Sessione WhatsApp Web scaduta / logout remoto | POM: assert sessione a inizio job; health-check cron | numero → `qr_required`; campagne del numero in pausa; alert Telegram | flusso QR §7.6 (cliente riscansiona); resume manuale |
| FM2 | DOM cambiato → selettori rotti | Selettore chiave non trovato N volte consecutive su chat diverse | stop invii del numero (NON marcare i contatti failed: è colpa nostra); campagna → `error`; alert + screenshot diagnostico | fix selettori (manutenzione POM); ripartenza: i `queued` restano queued |
| FM3 | Chat non trovata per numero (contatto non su WhatsApp / numero errato) | apertura chat fallisce in modo pulito (PoC-2 definisce il segnale) | contatto → `skipped` + `do_not_contact('invalid_number')`; catalogato per report cliente | nessuna (by design, decisione 4.4) |
| FM4 | Chat trovata ma **senza cronologia** (contatto freddo infiltrato nel CSV) | guardia V2 nel POM | `skipped('no_existing_chat')`, nessun invio | il cliente rivede la lista |
| FM5 | Invio non confermato (niente spunta / messaggio non in chat) | check post-invio best-effort | retry 1 volta nella stessa sessione; poi `failed` + retry backoff a sessione successiva | oltre soglia → `unreachable` |
| FM6 | Crash browser / Chromium zombie | eccezione Patchright; mutex per-profilo previene doppio browser | job → `Retry(defer)`; profilo riaperto al giro dopo | pattern context_manager esistente |
| FM7 | Proxy mobile giù | errori rete dal browser; probe proxy (riuso `proxy_probe.py`) | pausa invii del numero (NON failure dei contatti); alert | ripristino tether; resume |
| FM8 | Numero bannato/limitato da WhatsApp | messaggio/interstitial specifico nel DOM (da catalogare in PoC) | numero → `suspended`; campagne stop; alert URGENTE | gestione col cliente; post-mortem obbligatorio |
| FM9 | Umano sta scrivendo nella stessa chat durante lo step | check C4 pre-typing | step rinviato (`next_action_at += ore`) | automatico |
| FM10 | CSV sporco (numeri malformati, colonne rotte, encoding) | validazione ingest riga-per-riga | righe scartate con motivo nel report; l'ingest non fallisce in blocco | l'admin corregge e ricarica (dedup regge il doppio upload) |
| FM11 | Doppio worker sullo stesso numero | `_job_id` dedup ARQ + mutex profilo + lease (pattern IG) | il secondo job esce no-op | — |
| FM12 | Redis giù | ARQ non processa; enqueue fallisce | invii fermi (fail-safe: niente perso, `queued` resta in DB); alert health | restart Redis; tick riprende |
| FM13 | DB/Supabase blip | `is_transient_db_error` | `Retry(defer=60)` (riuso db_resilience) | automatico |
| FM14 | PC riavviato / blackout | profili e DB persistenti; Redis persiste i job | al riavvio: startup guard (pattern worker DM) sana i `sending` stale → `queued` | recovery cron (riuso pattern `recovery_checker`) |
| FM15 | Kill-switch attivato | check interno in ogni job | tutto si ferma entro il job corrente | `/unhalt` riaccoda solo lavoro ancora attivo |

Principio trasversale (lezione IG): **distinguere sempre "colpa del contatto" (→ failed/DNC) da "colpa nostra/dell'infrastruttura" (→ pausa e retry, i contatti restano queued)**. Un selettore rotto non deve bruciare una lista.

---

## 12. Appendice GDPR

### 12.1 Ruoli (assetto probabile — DA VALIDARE COL LEGALE, decisione 4.1)

- **Cliente (es. Primero) = Titolare del trattamento**: decide finalità (marketing ai propri contatti) e mezzi; ha (o deve avere) la base giuridica verso i suoi contatti (consenso marketing o legittimo interesse su clienti esistenti + soft opt-in ePrivacy — valutazione sua/del suo legale).
- **Tommaso = Responsabile del trattamento (processor)**: tratta i numeri per conto del cliente. Serve **DPA** (art. 28) con ogni cliente: istruzioni documentate, misure di sicurezza, sub-responsabili (es. provider AI in fase 2), assistenza per diritti interessati, cancellazione a fine rapporto.
- L'SDD **documenta** questo assetto; **non lo decide**. Punto aperto per il legale, prima del go-live commerciale.

### 12.2 P12 — Pseudonimizzazione e minimizzazione (misura tecnica)

Il numero di telefono in chiaro esiste solo ai **due confini**:

```
   CSV (ingest) ──► [ parse in-memory ] ──► phone_hmac  = HMAC-SHA256(E164, WA_HMAC_KEY)
                                        └─► encrypted_phone = Fernet(E164)
   ─────────────────────────────────────────────────────────────────────────
   INTERNO (DB chiavi, log, eventi, stats, futura AI): SOLO phone_hmac
   log/display: forma mascherata  +39•••••077
   ─────────────────────────────────────────────────────────────────────────
   invio ──► wa_sender decifra encrypted_phone ──► WhatsAppWebPage apre chat
             (in-memory, mai loggato)
```

- `WA_HMAC_KEY`: chiave dedicata in `.env` (distinta da `SECRET_KEY` Fernet). Rotazione = re-HMAC di tutte le righe → procedura documentata, non prevista di routine.
- **PII-masking pre-AI** (fase 2): prima di mandare qualsiasi contesto a un provider AI, strip di numeri/email/indirizzi dal testo. Vincolo ereditato da TheVista: **no provider non-UE su PII** senza DPA adeguata.
- **Onestà tecnica**: il dato pseudonimizzato resta dato personale (il sistema può re-identificare). P12 è minimizzazione/sicurezza, NON anonimizzazione, e non sostituisce base giuridica né DPA.

### 12.3 Minimizzazione dei contenuti

- MVP **non salva conversazioni**: solo preview troncata dell'ultimo inbound (`wa_inbound_events.preview_text`), necessaria per opt-out; scartabile dopo processing (retention breve, Q92).
- `wa_messages.rendered_text` (outbound) si salva per audit/retry: contiene il testo della promo + placeholder valorizzati (possibile PII leggera tipo nome). Retention da definire (Q92).
- KPI e analytics: solo aggregati e `phone_hmac`.

### 12.4 Diritti interessati e igiene

- **Opt-out** (§7.5) = attuazione tecnica del diritto di opposizione al marketing. Permanente, vince sugli ingest.
- **Cancellazione**: procedura per tenant ("cliente X chiude") = purge `wa_contacts`/`wa_messages`/`wa_inbound_events` del tenant + profili browser. Da scriptare (M5).
- **Registro trattamenti / informative**: carta a carico del titolare (cliente); Tommaso fornisce descrizione tecnica del trattamento (questo capitolo ne è la base).
- **Data breach**: il PC che ospita le sessioni è l'asset critico (T9) — misure fisiche/cifratura da decidere (Q99).

---

## 13. PoC gate (go/no-go)

Ordine sequenziale: ogni PoC sblocca il successivo. Il fallimento non recuperabile di PoC-1/2/3 rimette in discussione la strada A (→ riconsiderare B, con occhi aperti).

| PoC | Cosa prova | Setup | Criteri GO (misurabili) | Criteri NO-GO |
|---|---|---|---|---|
| **PoC-1 — Sessione persistente** | Login QR una volta → sessione dura su profilo Chromium persistente; riavvii PC/browser inclusi | Numero di test (SIM dedicata di Tommaso, NON un numero cliente), profilo Patchright, proxy mobile | Sessione viva ≥ **14 giorni** con uso quotidiano; sopravvive a ≥ 5 riavvii browser e ≥ 2 riavvii PC; nessun re-scan richiesto | Re-scan richiesto > 1 volta/settimana senza causa identificabile |
| **PoC-2 — Invio in chat esistente** | Aprire chat per numero (search interna e/o deep-link `web.whatsapp.com/send?phone=`) e inviare, indipendente dall'ordine lista | chat pre-esistenti col numero test | 50/50 invii riusciti su chat esistenti diverse; metodo di apertura deterministico scelto; segnale affidabile per "chat inesistente" e "chat senza cronologia" (guardia V2); check spunta post-invio caratterizzato | apertura per numero non affidabile (< 90%) o indistinguibile fallimento/successo |
| **PoC-3 — Lettura risposte da DOM** | Rilevare inbound **dalla sola lista chat** (unread badge + preview + title), associare al contatto, senza aprire chat | scenari: contatto salvato in rubrica (title=nome) e non salvato (title=numero); risposta letta prima dall'umano | 20/20 risposte rilevate entro 1 ciclo di scan; matching corretto nei 2 scenari; comportamento documentato nel caso "umano ha già letto" | il DOM non espone abbastanza per il matching o lo scan richiede di aprire le chat |
| **PoC-4 — Coesistenza** | Bot invia mentre l'umano usa app dal telefono; umano scrive nella chat che il bot sta per toccare | 2 persone: una fa l'umano-business | nessun doppio messaggio; check C4 funziona; nessuna anomalia visibile lato telefono (sessione non buttata fuori) | interferenze sistematiche o logout del telefono |
| **PoC-5 — Volume/stress** | Ritmo da produzione per giorni | 100-200 msg/giorno × ≥ 5 giorni sul numero test, timing §10.3 | zero warning/limitazioni WhatsApp; sessione stabile; error rate invii < 5%; CPU/RAM per sessione misurate (→ verifica A5) | warning WhatsApp o instabilità sistematica |

Output dei PoC: `docs/whatsapp/poc-report.md` con esiti, selettori catalogati, segnali di errore censiti (base per il POM definitivo).

---

## 14. Roadmap a milestone

Percorso: PoC → fondamenta → MVP → collaudo. Ogni milestone di codice segue skill `sviluppo-modulo` (subagent, QA funzionale + adversarial, fix loop) e worktree isolato + branch + PR (git-hygiene). Le stime sono in sessioni di lavoro (ordine di grandezza, da raffinare nello spec/plan).

```
 M0 ──► M1 ──► M2 ──► M3 ──► M4 ──► M5 ──► (fase 2: F1..F7)
 PoC    dati   ingest invio  reply  QA+
 gate   +POM   +camp. engine +brand collaudo
                             +optout Primero
```

| Milestone | Contenuto | Include | Gate di uscita |
|---|---|---|---|
| **M0 — PoC gate** | PoC-1..5 su numero test; catalogazione selettori/segnali; misura RAM per sessione | script PoC usa-e-getta (non codice prodotto) | tutti GO (§13); report scritto |
| **M1 — Fondamenta** | migrazioni (tenants, wa_*), `phone_pseudonym`, `WhatsAppWebPage` v1 (sessione, apertura chat, invio), `human_input` estratto da `instagram_page`, `wa_session` + flusso QR admin | refactor estrazione human_input con test di non-regressione IG | POM supera test manuali su numero test; canale IG regredito zero |
| **M2 — Ingest + campagne** | `wa_ingest` CSV + report scarti, CRUD campagne/sequenze/numeri, frontend admin base | contratto ingest documentato (futuro F5) | campagna definibile end-to-end da UI, contatti caricati e dedupati |
| **M3 — Invio** | `wa_sender` + `wa_worker` (mini-sessioni, claim, caps, defer), `wa_number_manager` (warmup/cap date-aware), pacing, kill-switch, eventi | guardia V2, check C4, failure FM1-FM7 | campagna 1-step gira su numero test con cap basso; KPI inviati/falliti corretti |
| **M4 — Reply, branching, opt-out** | `wa_reply_watcher` + cron, matching contatto, `wa_sequence_engine` completo (tick, condizioni, wait_days), opt-out end-to-end, DNC/catalogazione non-raggiungibili | eventi webhook-ready (`wa.*`) | sequenza 3-step con branching reale verificata su numero test; STOP funziona |
| **M5 — QA + collaudo Primero** | QA adversarial (skill `sviluppo-modulo`): lista funzionale + lista adversarial, fix loop 100%; script purge tenant (GDPR); runbook operativo (onboarding numero, QR, incident) | collaudo di Tommaso; poi prima campagna reale Primero con cap prudente | DoD §15 raggiunta |
| **Fase 2** | F1 flow builder/n8n, F2 UI cliente, F3 AI conversazione, F4 auto-reply, F5 ingest API, F6 analytics, F7 multi-numero | vedi `sviluppi-futuri.md` | — |

Dipendenze esterne alla roadmap: parere legale GDPR (12.1) — necessario prima del go-live commerciale multi-cliente, non blocca M0-M4 su numero test; acquisto SIM/proxy test; macchina con RAM adeguata (A5, misura in M0).

---

## 15. KPI e Definition of Done

### 15.1 KPI MVP (per campagna) — decisione 4.2

| KPI | Fonte |
|---|---|
| Inviati | `wa_messages.status='sent'` |
| Risposti | `wa_campaign_contacts.status='replied'` (+ `replied_at_step`) |
| Opt-out | `status='opted_out'` |
| Falliti / non-raggiungibili | `failed` + `do_not_contact('unreachable'/'invalid_number')` |
| Derivati | tasso risposta %, tasso opt-out %, non-raggiungibili % |

Dashboard: card KPI per campagna + contatori denormalizzati (pattern IG). Analytics ricche = F6.

### 15.2 Definition of Done MVP — decisione 4.3

Software pronto a lanciare una **campagna vera end-to-end**: ingest CSV reale → sequenza 3-step con branching → invii con pacing anti-ban → risposte rilevate → opt-out onorato → KPI corretti. Tutte le logiche testate + **QA adversarial come da skill `sviluppo-modulo`** (QA agent che prova fisicamente, lista funzionale + lista adversarial dove PASS = il sistema si difende, fix loop al 100%), collaudo finale di Tommaso. **Primo banco di prova: Primero.**

---

## 16. Backlog tecnico

Debiti/refactor consapevoli creati o rimandati da questo design (distinti dal backlog funzionale in `sviluppi-futuri.md`):

| # | Voce | Perché rimandata | Trigger di ripresa |
|---|---|---|---|
| BT1 | Unificazione fisica campagne (colonna `channel` su `campaigns`, UI unica) | D2b protegge il canale IG in prod | quando la UI multi-canale diventa fastidiosa da mantenere doppia |
| BT2 | Tenancy sul canale IG (oggi mono-tenant legacy) | non serve finché IG ha un solo cliente/uso | primo cliente IG ≠ WhatsApp |
| BT3 | Generalizzazione `account_manager` (oggi: copia del pattern in `wa_number_manager`) | generalizzare codice in prod = rischio senza beneficio immediato | terza implementazione del pattern (terzo canale) |
| BT4 | Tabella contatti unificata cross-canale | V4 dice identità per-canale; unificare ora = astrazione prematura | solo se emerge un requisito reale cross-canale |
| BT5 | Bridge webhook in/out per n8n | fase 2 (F1); MVP emette già eventi `wa.*` | avvio F1 |
| BT6 | Monitor automatico salute selettori (canary check giornaliero del POM su chat di test) | in MVP basta FM2 (stop su fallimenti consecutivi) | primo incidente da DOM cambiato in produzione |
| BT7 | Retention/purge automatica `wa_inbound_events.preview_text` e `rendered_text` | serve decisione retention (Q92) | esito valutazione legale |
| BT8 | Cifratura disco / hardening macchina sessioni (T9) | decisione infra di Tommaso | prima del multi-cliente reale |
| BT9 | Naming piattaforma (il progetto non è più solo "BOT OUTBOUND") | puro branding, zero urgenza tecnica | quando serve presentarla ai clienti |

---

## 17. Domande di validazione residue

Domande aperte, raggruppate. **[T]** = decide Tommaso (prodotto/business) · **[PoC]** = risponde il PoC gate · **[L]** = legale · **[S]** = si decide nello spec/plan di milestone.

### A. Prodotto e cliente

1. [T] Il cliente fornisce sempre WhatsApp **Business** (app), o dobbiamo supportare anche WhatsApp consumer? (Linked device e limiti potrebbero differire.)
2. [T] Una campagna per numero alla volta, o più campagne concorrenti sullo stesso numero? (MVP assume: più campagne possibili ma cap numero condiviso — confermare.)
3. [T] Il report "non-raggiungibili/catalogati" al cliente: formato? (CSV export? vista dashboard? per l'MVP basta la vista?)
4. [T] Chi scrive i testi delle campagne MVP: il cliente, Tommaso, o insieme? (Impatta T6 e il contratto.)
5. [T] La CTA opt-out standard va bene fissa per tenant o serve per-campagna?
6. [T] Lingue: solo italiano in MVP? (STOP-regex, template, finestra oraria.)
7. [T] Il cliente deve poter vedere QUALCOSA in MVP (anche read-only), o zero accesso fino a F2?
8. [T] Prezzo per messaggio: si fattura su `sent` confermati o su tentati? (Impatta quanto investire sul delivery_check.)
9. [T] Cosa promettiamo contrattualmente sul rischio ban del numero cliente? (Disclaimer necessario.)
10. [T] Un contatto può essere in più campagne attive dello stesso tenant contemporaneamente? (MVP propone: no — frequency cap lo impedisce di fatto. Confermare.)
11. [T] Serve un "test send" (invio di prova a un numero interno) prima dello start campagna? (Proposta: sì, feature piccola e preziosa.)
12. [T] Il killer use case Primero: quale primo caso concreto (promo? riattivazione 6 mesi? evento)? Serve per tarare template e KPI attesi.

### B. Ingest e dati

13. [S] Formati CSV accettati: separatore, encoding, header obbligatori — quale contratto esatto?
14. [S] Normalizzazione numeri: default paese +39 se manca il prefisso? Numeri esteri accettati?
15. [T] Colonne libere: limite al numero/dimensione degli attributi? (Proposta: JSON ≤ 2KB per contatto.)
16. [S] Re-upload dello stesso CSV con attributi cambiati: aggiornare `attributes` dei contatti esistenti o ignorare? (Proposta: aggiornare, gap-fill come `global_contacts`.)
17. [T] Il `nome` dal CSV vs il nome che il cliente ha in rubrica WhatsApp possono divergere: quale usare nei template? (Proposta: sempre quello CSV — è quello che il titolare conosce.)
18. [S] Cancellare contatti da una campagna dopo l'ingest (rimozione manuale singola): serve in MVP?
19. [PoC] `chat_title` è stabile nel tempo? (Se il cliente rinomina il contatto in rubrica, il matching del watcher si rompe → serve re-learn del title a ogni invio.)
20. [S] Dedup: due tenant con lo stesso numero contatto = due `wa_contacts` distinti (scoping per-tenant). Confermato dal modello — verificare che nessuna query rompa l'isolamento.
21. [S] Import parziale fallito a metà (crash durante ingest): transazionalità — tutto-o-niente o riga-per-riga con resume? (Proposta: riga-per-riga idempotente, il re-upload sana.)
22. [T] Massimo contatti per campagna in MVP? (Proposta: soft limit 5.000 — oltre, i tempi con cap 100-200/giorno diventano mesi: va detto al cliente.)
23. [S] `wa_contacts` orfani (mai in nessuna campagna, arrivati da CSV più larghi): tenerli o scartarli all'ingest? (Proposta: l'ingest crea solo i contatti della campagna — niente anagrafica speculativa. Minimizzazione.)
24. [S] Chi calcola `wait_days`: giorni di calendario o giorni "attivi" (finestre orarie)? (Proposta: calendario, semplice.)

### C. Sequenze e branching

25. [T] **D2 (§5.2): confermare D2b** (tabella `wa_campaigns` dedicata, UI unificata logicamente) vs D2a (riga in `campaigns`).
26. [T] Numero massimo step per sequenza in MVP? (Proposta: 5.)
27. [T] `wait_days` minimo consentito? (Proposta: 1 giorno — niente step a distanza di ore in MVP, riduce rischi T1.)
28. [S] Modifica sequenza a campagna `running`: consentita? (Proposta: solo per step non ancora raggiunti da nessun contatto; altrimenti pausa-modifica-riprendi.)
29. [T] Confermare la semantica §7.4: qualunque risposta ferma la sequenza del contatto (salvo step `if_replied` espliciti)?
30. [S] `if_replied` con risposta arrivata PRIMA dello step (es. risponde a msg1 dopo che msg2 era già partito): la risposta "vale" per quale step? (Proposta: `replied` è uno stato del contatto, non dello step — vale da lì in poi.)
31. [S] Ripartenza campagna dopo `paused` lunga: i `next_action_at` scaduti creano un burst — serve re-spalmatura? (Proposta: sì, re-pacing automatico al resume.)
32. [T] Step "attendi X giorni poi manda comunque" (`always` con wait) serve in MVP o basta condizionale? (Il modello lo supporta gratis — confermare che è desiderato.)
33. [S] Fuso orario: tutte le date in UTC nel DB, finestra oraria in ora locale del tenant. Confermare gestione DST.
34. [S] A/B per step (template A-D): il KPI per-variante (come `template_variant` IG) è richiesto in MVP o solo log?
35. [T] "Campagna test" con cap 5-10 msg: modalità dedicata o è solo un cap basso? (Proposta: solo cap basso, zero feature.)
36. [S] Contatto `failed` a metà sequenza che torna raggiungibile: la sequenza riprende dallo step fallito o si chiude? (Proposta: riprende dallo step fallito entro N giorni, poi chiude.)

### D. DOM, POM e browser

37. [PoC] Metodo di apertura chat per numero: search interna vs deep-link `/send?phone=` — quale regge meglio? Il deep-link su numero SENZA chat esistente cosa mostra esattamente? (Serve per la guardia V2.)
38. [PoC] Segnale DOM affidabile per "questa chat ha cronologia" (V2): quale?
39. [PoC] Segnale DOM per spunte (orologio/1/2) sull'ultimo messaggio inviato: leggibile in modo stabile?
40. [PoC] La lista chat virtualizza le righe (rendering solo visibili)? Quanto scroll serve per coprire le chat rilevanti del giorno?
41. [PoC] Badge unread: esposto come testo/aria-label o solo classe CSS? Robustezza del selettore.
42. [PoC] Direzione ultimo messaggio (inbound/outbound) leggibile dalla preview lista? (Serve a C4 e al miss-detection di 7.3.)
43. [PoC] WhatsApp Web ha aggiornamenti forzati ("Aggiorna WhatsApp Web")/interstitial che bloccano la pagina? Come si presentano nel DOM?
44. [PoC] Selettori: WhatsApp Web usa classi offuscate/instabili — puntare a `aria-*`/`data-testid`/ruoli? Catalogare in PoC la strategia meno fragile.
45. [S] Lingua interfaccia WhatsApp Web: forzare l'account/browser a lingua fissa (en o it) per stabilità selettori testuali? (Lezione IG: pagina-morta solo-EN.)
46. [PoC] Popup/tooltip/promo di WhatsApp Web (nuove feature, backup): quali esistono e come dismissarli (pattern dismiss IG)?
47. [PoC] `web.whatsapp.com` su Chromium Patchright con profilo persistente: serve user-agent particolare o va liscio?
48. [S] Screenshot diagnostici su failure: dove salvarli, quanta retention, contengono PII (schermata chat!) → cifrarli o mascherarli?
49. [PoC] Typing nel box messaggi: newline con Shift+Enter come su IG? Incolla da clipboard rilevabile vs typing? (MVP: sempre typing umanizzato.)
50. [PoC] Tempo medio di apertura chat + invio: quanto costa un messaggio in secondi? (Dimensiona i cap giornalieri realistici.)
51. [PoC] RAM/CPU per sessione Chromium con WhatsApp Web aperto stabile (verifica A5 con numeri veri).
52. [S] Il watcher e il sender condividono lo stesso browser/pagina del numero (mutex) — o pagine separate stesso profilo? (Proposta: stessa pagina, un solo contesto per numero, azioni serializzate dal mutex.)

### E. Sessione e dispositivo

53. [PoC] Durata reale sessione linked device su profilo persistente (A1): giorni? settimane? cosa la uccide (inattività? aggiornamento app telefono?).
54. [PoC] Limite dispositivi collegati per numero Business (A7): quanti slot, cosa succede al collegamento N+1?
55. [PoC] Il telefono del cliente spento/offline a lungo: il linked device continua a funzionare? Per quanto?
56. [S] Health-check sessione: frequenza (proposta: ogni 30 min nelle ore attive) e segnale minimo (selettore della lista chat presente?).
57. [S] Flusso QR remoto: il QR scade ~30-60s — meccanica del refresh sulla pagina admin e UX per il cliente al telefono. Dettagliare in M1.
58. [T] SLA di rilevamento risposta: ogni quanto gira `wa_reply_scan`? (Proposta: 15-30 min nelle ore attive — abbastanza per `wait_days`, abbastanza rado da pesare poco. Impatta anche il miss "STOP seguito da altro messaggio".)
59. [S] Un numero già collegato a un altro WhatsApp Web del cliente (suo PC): conflitto o coesistenza tra linked devices? (Dovrebbe coesistere — verificare in PoC-1.)
60. [T] SIM del numero test PoC: quale numero si usa? (Serve SIM dedicata che Tommaso controlla, con chat pre-esistenti simulabili.)

### F. Anti-ban e timing

61. [PoC] I parametri §10.3 reggono PoC-5? Tarare su dati.
62. [T] Warmup anche per numeri "anziani" del cliente: confermare la rampa (il numero è vecchio ma il pattern-bot è nuovo).
63. [S] Stagger tra numeri dello stesso proxy (V9: 2 numeri/IP): offset random come IG?
64. [PoC] WhatsApp mostra warning espliciti pre-ban (es. "messaggi segnalati")? Catalogare i segnali early-warning per FM8.
65. [T] Soglia allarme opt-out 5% (§10.3): confermare o tarare.
66. [S] Pausa automatica campagna su N failed consecutivi (pattern IG): quale N?
67. [T] Invii sabato/domenica: default off? (Proposta: configurabile per tenant, default off per marketing.)
68. [S] Distribuzione oraria dentro la finestra: uniforme-lognormale o con "picchi umani" (pausa pranzo)? (Proposta MVP: lognormale semplice, riuso human_behavior.)
69. [PoC] Il numero risulta "online" mentre il bot è collegato? Il cliente lo accetta? (Percezione lato contatti: "il negozio è sempre online".)
70. [S] Cap globale piattaforma (tutti i tenant) per la macchina: serve un tetto totale invii/giorno? (Proposta: sì, safety valve config.)
71. [T] Messaggi con media (immagine promo) in MVP o solo testo? (Proposta: solo testo — media = superficie di rischio e complessità in più; fase 2.)
72. [S] Link nei messaggi (URL promo): accorciati o nudi? I link accorciati sono un classico segnale spam — proposta: URL nudi del dominio del cliente.

### G. Coesistenza

73. [PoC] C3: aprire la chat per inviare quando c'è un unread — la rivalutazione pre-invio (§9) è implementabile in modo affidabile?
74. [S] C4: soglia "outbound umano recente" — quante ore? (Proposta: 4h.)
75. [PoC] L'invio bot mentre l'umano ha la STESSA chat aperta sul telefono: effetti visibili? (Il messaggio appare "scritto da solo" sul suo schermo aperto.)
76. [T] Va detto al cliente di NON usare WhatsApp Web suo in parallelo sulla stessa chat mentre gira una campagna? (Regola operativa da runbook.)
77. [S] L'umano cancella/archivia una chat che il bot deve toccare: comportamento? (Archiviata: il deep-link la riapre? PoC. Cancellata: cronologia persa → guardia V2 la skippa — corretto?)
78. [T] Il cliente mette il numero in "away/absence message" automatico di WhatsApp Business: interferenze col watcher? (L'auto-reply del Business è outbound automatico non-bot… nostro. Da osservare in PoC-4.)
79. [S] Etichette/liste broadcast preesistenti del cliente nell'app Business: fuori perimetro, ma confermare che non interferiscono.
80. [T] Formazione minima del cliente (runbook 1 pagina): cosa deve/non deve fare durante una campagna. Chi la scrive → M5.

### H. Multi-tenant e sicurezza

81. [S] Scoping tenant: enforcement a livello query (filtro ovunque) — pattern di guardia unico (dependency FastAPI che inietta tenant_id)? Da definire in M1.
82. [S] Auth MVP: riuso `api/auth.py` esistente con solo utente admin — confermare che regge (niente ruoli-tenant fino a F2).
83. [T] Isolamento file: profili browser per-numero già isolati per path; serve di più (permessi OS)?
84. [S] `WA_HMAC_KEY` e `SECRET_KEY`: rotazione documentata nel runbook; backup chiavi (perdere la Fernet = perdere i numeri cifrati).
85. [S] Backup DB: le tabelle wa_* entrano nel backup esistente Supabase; il purge-tenant (§12.4) deve raggiungere anche i backup? (Risposta legale tipica: retention backup limitata e documentata — [L].)
86. [T] Accesso remoto di Tommaso alla macchina sessioni (se non è il PC principale): come? (RDP/Tailscale — decisione infra.)
87. [S] Log applicativi: audit che nessun logger stampi numeri in chiaro — test dedicato in QA (grep sui log dopo run E2E).
88. [S] `.env`: nuove variabili (`WA_HMAC_KEY`, caps default, scan interval) → `.env.example` aggiornato, mai committare valori.

### I. GDPR e legale

89. [L] Validare assetto ruoli §12.1 (titolare=cliente, processor=Tommaso) + template DPA da far firmare ai clienti.
90. [L] Base giuridica marketing WhatsApp verso clienti esistenti del titolare: consenso esplicito o legittimo interesse/soft opt-in? (Responsabilità del titolare, ma Tommaso deve saperlo per il contratto.)
91. [L] L'automazione su WhatsApp Web viola i ToS WhatsApp (rischio contrattuale, non GDPR): come si riflette nel contratto col cliente? (Chi si assume il rischio ban → Q9.)
92. [L/T] Retention: `preview_text` (proposta: 30 giorni), `rendered_text` (proposta: durata campagna + 90 giorni), KPI aggregati (indefinita). Validare.
93. [L] Informativa del titolare ai contatti: serve menzione del processor/strumento? (Carta del cliente; fornire descrizione tecnica standard.)
94. [S] Procedura diritto d'accesso/cancellazione del singolo contatto (interessato chiede al titolare → titolare chiede a Tommaso): script per-contatto oltre al purge per-tenant.
95. [L] Fase 2 AI (F3): DPA con provider AI + localizzazione UE — riesaminare prima di attivare, vincolo già noto da TheVista.
96. [L] Trasferimento dati nel flusso QR remoto (screenshot QR via canale X al cliente): canale sicuro raccomandato (proposta: la pagina admin dietro auth, niente screenshot via WhatsApp/mail).

### J. Ops e infra

97. [T] Macchina: PC dedicato nuovo (16-32GB) o si parte sul PC attuale per M0-M3? (Nota: PC attuale = 7,4GB RAM, 3-4 sessioni max — per PoC basta, per 10 clienti no.)
98. [T] Proxy: quante SIM/telefoni tether servono per partire (test + Primero)? Budget?
99. [T] Cifratura disco della macchina sessioni (T9/BT8): BitLocker basta?
100. [S] Avvio servizi: i worker WA entrano in `start.bat`/`start.sh` esistenti o processo separato? (Proposta: stesso ARQ worker esistente + coda dedicata, un solo runtime.)
101. [S] Monitoraggio: health endpoint esteso (`api/health.py`) con stato sessioni WA; alert Telegram già coperti da notifier — definire la lista alert minima (QR, selettori, ban, proxy).
102. [S] Aggiornamenti Chromium/Patchright: policy di update (pinnare versione, aggiornare a mano dopo test su numero test).
103. [S] Timezone macchina vs tenant: tutto UTC internamente (lezione clock-skew TheVista).
104. [T] Chi fa da "umano di test" nei PoC-4 (serve una seconda persona col telefono)?
105. [S] Numero test: chat pre-esistenti da creare a mano prima dei PoC (seed realistico: 30-50 chat).
106. [S] Disaster recovery profilo browser corrotto: backup periodico della cartella profilo? (Proposta: sì, zip giornaliero, cifrato.)

### K. Business e go-to-market

107. [T] Prezzo per messaggio: quanto sotto Meta (€0,05-0,09)? Fatturazione mensile a consuntivo su `sent`?
108. [T] Setup fee per onboarding numero (SIM/proxy/QR/config)?
109. [T] Contratto tipo: chi lo scrive, cosa copre (rischio ban Q9/Q91, DPA Q89, responsabilità contenuti T6)?
110. [T] Primero come primo cliente: gratis/beta o pagante ridotto? Timeline desiderata per la prima campagna reale?

---

## 18. Riferimenti

- `docs/whatsapp/00-problematiche-e-decisioni.md` — registro decisioni brainstorming (fonte delle decisioni citate come "23/07")
- `docs/whatsapp/sviluppi-futuri.md` — backlog fase 2+ (F1-F7)
- `docs/project/PROGRESS.md` — voce [2026-07-23]
- `CLAUDE.md` (root repo) — architettura piattaforma esistente, principi anti-detection, schema DB IG
- Blueprint codice: `backend/app/services/browser_bio.py` (mini-sessioni browser), `backend/app/browser/instagram_page.py` (stile POM), `backend/app/services/import_resolver.py` (ingest difensivo)
- Skill `sviluppo-modulo` — protocollo sviluppo + QA adversarial (DoD §15.2)
