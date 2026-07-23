# Canale WhatsApp — Problematiche & Decisioni (living doc)

> Stato: **brainstorming CHIUSO** · Data: 2026-07-23 · Owner: Tommaso
> Questo NON è l'SDD. È il registro vivo delle criticità e delle decisioni preso durante il brainstorming.
> ➡️ **SDD scritto: [`SDD-whatsapp-channel.md`](SDD-whatsapp-channel.md)** (18 sezioni, diagrammi/sequence/state, threat model, roadmap, 110 domande residue). Questo doc resta la fonte delle decisioni; l'SDD le traduce in specifica.

---

## 1. Scopo del canale (frame corretto)

Estensione di BOT OUTBOUND: un **secondo canale** oltre a Instagram. Il progetto potrebbe smettere di chiamarsi "BOT OUTBOUND" (diventa piattaforma outreach multi-canale).

**Cosa È:** campagne di **marketing / re-engagement** verso persone che hanno **già scritto al business e avuto una conversazione** (contatti CALDI, chat già esistenti). Killer use case: **riattivare vecchi clienti**.

**Cosa NON È:** ~~outbound a freddo~~ (liste comprate/scrapate, numeri mai contattati). Questo azzera i rischi tipici del cold outbound su WhatsApp.

**Conseguenza:** buona parte delle criticità "classiche" da automazione WhatsApp (ban da cold, creazione chat con numeri nuovi, warm-up aggressivo, search-vs-scroll) **decadono o si riducono molto**. Le criticità che restano sono altre (sotto).

---

## 2. Decisione di architettura: la strada tecnica

Tre modi per far parlare un bot dal numero WhatsApp del cliente:

| | Cos'è | Pro | Contro |
|---|---|---|---|
| **A — Automazione browser (Patchright)** | Un Chrome reale con WhatsApp Web dentro; il bot "muove il dito". Si collega come **dispositivo aggiunto** → il cliente continua a usare il telefono normalmente. | Costo d'invio ~0; coesiste con l'uso umano del numero; usa le chat esistenti così come sono; riusa lo stack anti-detect già collaudato su IG. | Detection/fragilità DOM; pesante (1 browser per numero); ban residuo se marketing spammoso. |
| **B — Libreria non ufficiale (Baileys/whatsapp-web.js)** | Parla il protocollo WhatsApp senza (Baileys) o con (wwjs) browser nascosto. Come faceva probabilmente GoHighLevel. | Leggera, scala su tanti numeri; coesiste come dispositivo collegato. | Client **non ufficiale** → vettore ban; si rompe se cambia il protocollo. **Tommaso: ~sicuro venga intercettata subito.** |
| **C — WhatsApp Business API ufficiale (Cloud API)** | Via sanzionata da Meta. | Ban zero, stabile, scalabile, fatta per il marketing. | **Prende possesso del numero → il cliente perde l'app WhatsApp normale**; template pre-approvati; **paghi per messaggio marketing** (~€0,05-0,09/msg IT). |

### Decisione: **STRADA A** (browser automation, Patchright). Confidenza ~90%.

Motivazioni:
1. **Il cliente vuole tenere l'app WhatsApp normale sul numero** (risponde a mano dal telefono) → C esclusa (gli toglie l'app). A/B coesistono come dispositivo collegato.
2. **Modello di ricavo di Tommaso = far pagare per messaggio, SOTTO il costo Meta** → funziona solo se il costo d'invio è ~0 → C economicamente incompatibile (ti mette sopra Meta, non sotto).
3. **B (Baileys) valutata e scartata**: quasi certamente intercettata subito come client non ufficiale. Documentata per completezza, non adottata. *(Resta un'opzione da riconsiderare solo se A si rivelasse insostenibile.)*

### Pricing API ufficiale (per memoria, sul perché C è fuori)
- Non paghi le risposte ricevute.
- Da luglio 2025 Meta paga **per messaggio template inviato**. Conversazioni di **servizio** (avviate dal cliente) gratis; messaggi **marketing** (avviati dal business) = categoria più cara, per messaggio. → incompatibile col modello di ricavo.

---

## 3. Riuso da BOT OUTBOUND (esplorazione repo, 2026-07-23)

Non è un sistema nuovo: è un **canale nuovo** su piattaforma esistente. Stima ~50-60% del lavoro riusabile "as-is".

| Componente | Path | Verdetto |
|---|---|---|
| Template A/B/C/D + spintax | `backend/app/services/template_renderer.py`, `ai_personalizer.compose_message` | RIUSABILE as-is |
| Pool browser + profili persistenti + anti-fingerprint JS | `backend/app/browser/context_manager.py`, `fingerprint.py` | RIUSABILE as-is |
| Timing lognormale + SessionManager (orari/break) | `backend/app/utils/timing.py`, `services/human_behavior.py` | RIUSABILE as-is |
| Warmup/cooldown/caps, retry, db-resilience, kill-switch | `services/account_manager.py`, `utils/retry.py`, `utils/db_resilience.py`, `models/bot_state.py` | RIUSABILE as-is |
| AI multi-provider + failover | `services/ai_personalizer.py`, `adapters/ai.py` | RIUSABILE as-is (prompt da ritoccare) |
| Scheletro worker ARQ fan-out/stagger/lease/defer/recovery | `workers/task_queue.py`, `services/work_enqueue.py`, `account_lease.py` | RIUSABILE as-is (scheletro) |
| Umanizzazione input (typing lognormale, typo QWERTY, click, browse) | primitive dentro `browser/instagram_page.py` | DA ADATTARE (estraibili) |
| Campaign engine (loop, claim atomico, dedup, limiti live) | `services/campaign_orchestrator.py`, `reservation.py` | DA ADATTARE (cablato su `Follower`/stati IG) |
| Schema DB (contatto/campagna/messaggio/stati) | `models/*`, `services/global_contact_service.py` | DA ADATTARE (**identità `ig_user_id` BigInt → numero di telefono stringa**) |
| Estrazione contatti (già ha `whatsapp`/`phone`/`wa.me`) | `utils/contact_extract.py` | DA ADATTARE |
| Reply handling | `services/reply_checker.py` | DA ADATTARE (pattern sì, transport no) |
| Page Object Model (selettori/URL/flusso) | `browser/instagram_page.py` | DA RISCRIVERE → `WhatsAppWebPage` |
| Scraping/reply via instagrapi | `services/scraper.py`, `reply_checker.py` | SPECIFICO-IG, non riusabile |

**Blueprint diretto per il worker WhatsApp:** `backend/app/services/browser_bio.py` (mini-sessione browser per-account + claim atomico + `Retry(defer)` + soft-block escalation). Ricalca ciò che serve per l'invio WhatsApp Web.

**Dato favorevole:** su IG l'inbox si legge solo via API (il DOM mostra solo il nome). Su **WhatsApp Web il DOM espone nome + numero** → **reply-checker browser-based più fattibile che su IG**. Il vincolo "no API" qui pesa poco.

---

## 4. Problematiche aperte (frame caldo + strada A)

Le criticità morte col frame caldo NON sono qui. Restano queste:

- **P1 — Ban residuo da "marketing fatigue".** Anche a contatti caldi, mandare promo ripetute → qualcuno blocca/segnala → WhatsApp può bannare il numero su segnali spam. Caldo ≠ immune. Leve: spintax (già c'è), frequency cap, qualità contenuto, opt-out, volumi moderati.
- **P2 — Opt-out / STOP.** Nessuna gestione nativa. Va costruita: rilevare "stop/basta/cancellami" nelle risposte → marcare opted-out → escludere per sempre. Requisito anche legale (ePrivacy). Riusa pattern reply-checker.
- **P3 — GDPR.** (a) mandare PII del cliente (numero, nome, storico, testo chat) a un modello AI = trasferimento a fornitore → base giuridica + DPA (stesso problema di TheVista: no provider non-UE su PII); (b) profilazione per targeting = base giuridica + trasparenza; (c) consenso/base giuridica marketing + opt-out. L'AI amplifica (a)(b) ma il GDPR c'è anche senza AI.
- **P4 — Stabilità sessione / dispositivo collegato.** WhatsApp Web come linked device può disconnettersi / richiedere ri-scan QR; limite di dispositivi collegati per numero. Ops concern reale → PoC.
- **P5 — Coesistenza uso umano ↔ bot sullo stesso numero.** Il cliente scrive a mano dal telefono mentre il bot invia → race condition, spunte di lettura, il bot marca "letto" ciò che l'umano non ha visto. Servono regole di non-interferenza.
- **P6 — Matching identità.** Numero WhatsApp ↔ record CRM. Normalizzazione E.164 (già in `contact_extract`). La chat esistente va agganciata al contatto giusto; la lista chat mostra il nome-contatto, non sempre il numero.
- **P7 — Sequenze multi-step.** msg1 → se risponde → msg2 → se risponde → msg3. State machine per (contatto × campagna) con branching su risposta/tempo. È il cuore della "campagna flessibile" richiesta. Possibile integrazione n8n (da valutare: guadagna il posto o basta il campaign engine + webhook?).
- **P8 — Multi-tenant.** Tommaso vende a più clienti, ognuno con proprio numero/sessione browser/scope dati. Isolamento dati per cliente. Nuovo requisito rispetto a BOT OUTBOUND (oggi mono-tenant).
- **P9 — Integrazione CRM esterni.** Primero = sistema preciso (gestionale creato da Tommaso, integrabile via API). Altre aziende = incognita: API generica se c'è, altrimenti import CSV. Serve un layer di ingest flessibile (API + CSV).
- **P10 — HITL su AI.** All'inizio human-in-the-loop obbligatorio (draft → Tommaso/operatore approva → invio). Autonomia AI solo dopo validazione.
- **P11 — Fragilità DOM.** WhatsApp Web cambia interfaccia → selettori si rompono. Servono selettori robusti + monitoraggio + PoC di stabilità nel tempo.

---

## 5. PoC gate (go/no-go prima di costruire tutto)

- **PoC-1 — Sessione persistente:** login WhatsApp Web via QR una volta, sessione dura nel tempo su profilo Chromium persistente; recovery se cade.
- **PoC-2 — Invio in chat esistente:** aprire una chat già esistente (per numero/nome) e inviare, in modo stabile, senza dipendere dall'ordine lista.
- **PoC-3 — Lettura risposte da DOM:** rilevare nuovi messaggi in ingresso (MutationObserver o polling leggero) e associarli al contatto/numero, stabile.
- **PoC-4 — Coesistenza:** bot + uso umano sullo stesso numero senza interferenze evidenti.
- **PoC-5 — Volume/stress:** qualche centinaio di messaggi/giorno con timing umano, sessione lunga, misurare stabilità e segnali di rischio.

---

## 6. Vincoli & assunzioni

- Strada A (browser), no API WhatsApp, no librerie protocollo non ufficiali (salvo riconsiderazione).
- Contatti caldi (chat esistenti), non cold.
- Numero = WhatsApp Business (app) del cliente; il cliente continua a usarlo dal telefono.
- Volume: max qualche centinaio msg/giorno per numero, potenzialmente ogni giorno.
- Modello ricavo: costo per messaggio, sotto Meta.
- Multi-tenant: più clienti; stesso cliente può usare anche il canale Instagram → piattaforma unificata.
- HITL su AI all'inizio.

---

## 7. Decisione architettura: MONO-PROGETTO MULTI-CANALE

**Stesso repo BOT OUTBOUND, che evolve in piattaforma outreach multi-canale.** NON un progetto separato con DB separato.

Motivazioni:
1. Riuso ~50-60% as-is → un progetto separato duplicherebbe l'anti-detect e divergerebbe (due codebase da mantenere = disastro).
2. Piattaforma unificata: stesso cliente può usare IG + WhatsApp, multi-tenant ~10 clienti → dashboard/tenant/campagne/worker condivisi.
3. Il lavoro vero è "canale nuovo" non "sistema nuovo": generalizzare l'identità, `WhatsAppWebPage`, layer ingest CRM.

**Identità non cross-canale:** un contatto appartiene a UN canale (identità IG *oppure* telefono). Il DB generalizza l'identità per supportare entrambi, senza merge cross-canale (telefono↔username IG non incrociabili, e va bene così). Semplifica.

**Organizzazione:** doc in `docs/whatsapp/` (isolamento a livello documenti); codice integrato in `backend/app/` (nuovi moduli `browser/whatsapp_page.py`, astrazione `channel`, servizi WhatsApp). Il progetto si rinomina concettualmente, non si spezza.

---

## 8. Esiti Tema 2 (modello operativo, campagne, infra)

- **Operatività (2.1):** interfaccia unica multi-tenant con viste per ruolo — vista **semplificata** per il cliente (self-serve) + vista **admin** per Tommaso. Managed-vs-self-serve = impostazione per-tenant (tier di prezzo), non due prodotti.
- **Targeting (2.7):** NON lo costruiamo. La segmentazione vive nel CRM del cliente. Noi **ingeriamo una lista già filtrata** (CSV export, o API se disponibile) = "manda *questa* campagna a *questi* numeri + dati opzionali per personalizzazione". Cancella la complessità di un motore di segmentazione.
- **Flow/sequenze (2.2, 2.3):** obiettivo builder **visuale** stile n8n/GoHighLevel, con **tanti flow modificabili per (tenant, campagna)**. Un flow = un record/definizione. Strade: (1) MVP config semplice `msg1→2→3` + branching base; (2) integrare **n8n** come motore di flow (n8n = cervello visuale, BOT OUTBOUND = esecuzione+anti-detect, dialogo via webhook); (3) build-own visual builder (over-engineering ora). **Raccomandazione: MVP = (1), ma invio/risposta come interfacce webhook-ready → n8n (2) si innesta in fase 2 quasi gratis.** Decisione finale: DA CONFERMARE (domanda 3.1).
- **AI in sequenze (2.4):** toggle on/off per campagna. Due modalità: (i) **follow-up AI**: prende chi non risponde da X giorni, **legge la conversazione** e genera follow-up personalizzato; (ii) **hook personalizzato**: rilegge la chat e aggancia qualcosa di detto nel messaggio campagna. Richiede una **skill/prompt** ben fatta (plausibile, no allucinazioni). Opzionale — si può anche solo mandare template fissi A/B/C/D come su IG. NOTA GDPR (P3): leggere+inviare conversazioni all'AI = PII a fornitore.
- **Coesistenza (2.9):** MVP = **umano-prima** (bot non auto-risponde agli inbound; il cliente risponde → umano gestisce; bot notifica/marca). Fase 2 = auto-reply con **timer anti-doppio-messaggio** (bot entro N min, poi umano) + lock che prima di inviare controlla se l'umano ha appena scritto.
- **Opt-out (2.10):** per **tipo campagna** — marketing → CTA "scrivi STOP" + gestione opt-out; follow-up → niente. Togglabile per attività/campagna. Scoped per-canale, mai cross-canale.
- **Scala (2.5):** ~10 clienti nei primi 6 mesi.
- **Infra (2.6):** un **PC fisico potente** (16-32GB RAM) con tutte le sessioni browser dedicate (modello Patchright IG). **Proxy mobili** via telefoni in tether USB + app proxy (es. EveryProxy). Da verificare capienza ~10 sessioni Chromium su una macchina.
- **Primero API (2.8):** nessuna API prevista ancora → per ora CSV export; endpoint dedicato eventualmente dopo.

---

## 9. Esiti Tema 3 + PERIMETRO MVP (fase 1)

**Perimetro MVP — cosa ENTRA:**
- Invio **sequenze semplici** (lista `msg1→2→3`) con **branching base**: ha risposto / non ha risposto / attendi X giorni. Set minimo di operatori nel flow.
- **Rilevamento risposte via DOM** (Patchright legge numero+nome+testo dallo schermo — su WhatsApp non esiste un middle-tier tipo instagrapi; DOM è l'unica via sicura, ed è fattibile). Serve a: pilotare il branching + registrare per statistiche di campagna (come su IG). **Nessuna notifica Telegram** (il cliente vede le risposte dalle notifiche di WhatsApp Business e gestisce in autonomia).
- Template fissi A/B/C/D + spintax + **placeholder da CSV**.
- **Ingest CSV**: unica colonna obbligatoria = **numero**; `nome` + N colonne libere usabili come placeholder (`{nome}`, `{ultimo_ordine}`…).
- **Cap invio basso** per testare, modificabile a mano (come su IG); warmup/caps riusati da `account_manager.py`.
- **Multi-tenant lato admin** (Tommaso opera tutto).

**Perimetro MVP — cosa NON entra (→ `sviluppi-futuri.md` + PROGRESS):** flow builder visuale/n8n, UI cliente self-serve, AI lettura-conversazione (2 modalità, legge ultimi ~10 msg — fase 2), auto-reply con timer anti-doppio-messaggio (coesistenza fase 2), ingest via API CRM, analytics avanzate, multi-numero esteso.

**Dettagli operativi confermati:**
- **Sessione/numero:** ogni campagna ha un **numero dedicato + sessione** browser. Se la sessione scade → si chiede al **cliente di riscansionare** il QR.
- **Proxy/SIM:** li fornisce **Tommaso** (costo ribaltato sul cliente). **1 proxy mobile (IP) ↔ max 2 numeri**, possibilmente della **stessa azienda**. Modello anti-detect come IG (Patchright + proxy mobili via telefoni tether USB + app tipo EveryProxy).
- **Coesistenza MVP:** umano-prima; il bot rileva la risposta e agisce sul flow (branch/stop), non notifica.

---

## 10. Log decisioni

- 2026-07-23 — Frame corretto: caldo/marketing/reactivation, non cold outbound.
- 2026-07-23 — Strada **A** (Patchright browser). B documentata e scartata. C esclusa (prende il numero + modello ricavo per-messaggio).
- 2026-07-23 — **Mono-progetto multi-canale** dentro repo BOT OUTBOUND (no progetto/DB separato). Identità per-canale, no merge cross-canale.
- 2026-07-23 — Doc in `docs/whatsapp/`, codice in `backend/app/`.
- 2026-07-23 — HITL su AI obbligatorio all'inizio. Coesistenza MVP = umano-prima.
- 2026-07-23 — Targeting = ingest lista CSV/API dal CRM del cliente (no motore segmentazione interno).
- 2026-07-23 — Opt-out per tipo campagna (marketing = STOP CTA; follow-up = no), togglabile, per-canale.
- 2026-07-23 — **Flow builder CONFERMATO:** MVP = sequenze semplici + branching base; invio/risposta come interfacce webhook-ready per innestare n8n in fase 2. Builder visuale/n8n → `sviluppi-futuri.md`.
- 2026-07-23 — MVP perimetro definito (sez. 9). Ingest CSV: solo numero obbligatorio. Rilevamento risposte via DOM, no Telegram. Numero+sessione per campagna, re-scan a carico cliente. 1 proxy ↔ max 2 numeri stessa azienda, proxy forniti da Tommaso.

---

## 11. Esiti Tema 4 (chiusura brainstorming)

- **GDPR ruoli (4.1):** DA VALUTARE COL LEGALE (assetto probabile: cliente = titolare/controller, Tommaso = responsabile/processor + DPA). L'SDD documenta l'assetto ma non lo decide.
- **P12 — Pseudonimizzazione & data minimization (idea Tommaso):** numero reale solo ai due confini (ingest CSV + invio browser); internamente (DB/log/stats/AI) chiave = **HMAC(numero, chiave_segreta)** deterministico. + **PII-masking prima dell'AI**. Riduce l'esposizione (misura di minimizzazione), NON sostituisce la valutazione legale (dato pseudonimizzato = ancora dato personale). Anonimizzazione totale impossibile (serve il numero reale per inviare).
- **KPI MVP (4.2):** inviati, risposti, opt-out, falliti (+ derivati gratis: tasso risposta %, non-raggiungibili).
- **Definition-of-Done MVP (4.3):** software pronto a lanciare una campagna vera end-to-end, tutte le logiche testate + **QA adversarial come da skill `sviluppo-modulo`** (QA agent + lista funzionale + lista adversarial, fix loop al 100%, collaudo Tommaso a MVP). Primo banco di prova: **Primero**.
- **Numeri falliti/non-raggiungibili (4.4):** marcare **"non contattare" + catalogare** (evita spreco risorse); eventuale report al cliente. → entra nel perimetro MVP.

**Brainstorming CHIUSO 2026-07-23.** Materiale sufficiente per l'SDD.

---

## 12. Log decisioni (agg. Tema 4)

- 2026-07-23 — GDPR ruoli → legale (probabile controller=cliente / processor=Tommaso + DPA).
- 2026-07-23 — **P12 pseudonimizzazione HMAC + PII-masking AI** come misura di minimizzazione (non sostituisce DPA).
- 2026-07-23 — KPI MVP: inviati/risposti/opt-out/falliti (+derivati). DoD = campagna reale + QA adversarial. Primo target Primero.
- 2026-07-23 — Falliti/non-raggiungibili → "non contattare" + catalogazione (perimetro MVP).

### Review SDD (23/07, sessione serale)

- **Opt-out garantito dalla guardia pre-invio**: a chat aperta, prima del typing, il bot legge gli inbound successivi al proprio ultimo messaggio (budget fisso: visibili + 1-2 scroll; costo target ≤2s, misura in PoC-2). STOP mai scavalcabile anche tra campagne distanti; lo scan lista resta solo come rete veloce durante campagne attive. Se il costo reale sfora → rivedere strategia.
- **Kill-switch separato per canale** (`wa_halted`): incidente WA non ferma IG e viceversa.
- **Max 1 campagna `running` per numero** → problema pacing cross-campagna eliminato alla radice.
- **UI: mondi separati, stessa shell** — stesso login, picker canale post-login; tema WA verde scuro (~#128C7E), IG spostato verso magenta/rosa. DB logicamente disgiunti (D2b confermato, i canali non comunicano).
- **`chat_title` salvato solo se è un nome** (mai numero in chiaro → P12); matching con title ambiguo (omonimi) → solo via numero, altrimenti evento non associato + alert. Mai indovinare.
- Non-goal espliciti aggiunti: no gruppi, no liste broadcast.

---

## 13. Prossimo passo
Scrivere l'**SDD completo** in `docs/whatsapp/` (decine di pagine: diagrammi ASCII component/sequence/state, casi d'uso, threat model, failure mode, roadmap milestone, backlog tecnico, ~100 domande di validazione residue). Poi spec/plan via workflow superpowers + skill `sviluppo-modulo` per l'implementazione.
