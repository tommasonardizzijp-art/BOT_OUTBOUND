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

### Decisioni [T] (24/07, sessione pre-M0) — SDD → v1.2

Sciolte le domande di §17 che bloccavano il PoC gate:

| Q | Decisione | Impatto |
|---|---|---|
| Q1 | Numero cliente = **WhatsApp Business** (app) | PoC girano su Business: away-message, etichette, slot linked device inclusi nel test |
| Q60 | Numero di test = **secondario di Primero**, non il principale. **Vincolo: solo messaggi reali, mai di test** (non si bruciano i contatti del cliente) | PoC-2 invia **solo a chat controllate** (numeri di Tommaso/conoscenti in rubrica); PoC-3 sfrutta anche gli inbound spontanei reali in sola lettura |
| — | **PoC-5 (volume) fuori da M0** → rampa in M5 (10→30→60→100/giorno, stop al primo warning) | 500-1000 messaggi finti = impossibile col vincolo sopra. **A6 resta non verificata fino a M5: rischio accettato** (BT12) |
| Q29 | Risposta qualsiasi ferma la sequenza. **Scope MVP = campagna a 1 messaggio**, schema sequenze completo ma motore multi-step non costruito (BT11). **STOP = tag DNC permanente per-tenant** | M4 si sgonfia; DoD §15.2 riscritta (single-shot); nessuna migrazione per accendere il branching dopo |
| Q97 | **PC attuale** per M0-M3 | PoC-1 richiede PC acceso e sessione collegata per 14 giorni; la misura RAM/CPU dimensiona l'acquisto dopo |
| Q104 | Umano di test PoC-4 = **Tommaso** + un suo secondo dispositivo/numero | Nessuna seconda persona da coordinare |
| Q105 | **Nessun seed**: il secondario ha già 30-100 chat reali; servono ≥6 chat controllate tra queste | — |
| Q4 | **Testi scritti da Tommaso**, cliente approva | Controllo anti-spam interno; lavoro ricorrente da prezzare |
| Q6 | Solo **italiano** | STOP-regex IT, `Europe/Rome` |
| Q71 | Solo **testo**, niente media | Meno superficie anti-ban e DOM |
| Q12 | Caso concreto Primero **rimandato** al collaudo M5 | Template/KPI attesi restano generici; M0-M4 non dipendono |
| Q107 | Prezzo **deciso dopo il collaudo**, su costi reali | Non blocca M0-M4 |

---

## 13. Decisioni di esecuzione M0 (26-27/07)

Prese durante l'avvio operativo del PoC. **Modificano** decisioni prese sopra.

| Q | Decisione | Data | Impatto |
|---|---|---|---|
| Q98 | **Nessun proxy in M0.** `POC_WA_PROXY` resta vuota | 26/07 | M0 esce col layer proxy **non validato**: va provato in M1/M3 prima di qualunque campagna pagante |
| Q97 | **Il PC si spegne di notte.** Heartbeat con `--nota "dopo riavvio PC"` a ogni riaccensione | 26/07 | ~14 riavvii invece dei ≥2 richiesti: criterio superato per costruzione, e in cambio si misura il caso d'uso più ostile. Se la sessione muore, **è un risultato**, non un incidente |
| **Q60 (sostituita)** | **Mittente = numero PERSONALE di Tommaso su WhatsApp Business**, non il secondario Primero. Destinatari = suoi contatti personali **avvisati** | **27/07** | vedi sotto |
| Q105 | Confermato nessun seed: le ≥6 chat controllate escono dalle chat reali del numero personale | 27/07 | — |
| — | Radice artefatti da `D:\wa-poc` a **`D:\dev\wa-poc`**; numeri reali in `poc.env`, caricato da `_common.py` | 27/07 | `D:\dev` verificato **non** repo git: i numeri non possono finire in un commit |

### Perché Q60 è cambiata, e cosa costa

**Causa:** Tommaso non ha accesso al numero secondario di Primero, né lo avrà nei prossimi giorni. M0 è il cammino critico (PoC-1 dura 14 giorni): aspettare fermava tutto.

**Cosa migliora.** Il vincolo originale *"solo messaggi reali, mai messaggi di test"* nasceva per non bruciare i contatti di un cliente vero. Su un numero personale con destinatari avvisati quel rischio **sparisce**, e con esso il vero driver dei ban WhatsApp: i report per spam dei destinatari. Un errore dello script ora colpisce chat di Tommaso, non di Primero.

**Cosa costa.** Se WhatsApp banna, il numero perso è **quello personale**. Rischio valutato **basso ma non nullo**:
- a favore: protocollo linked-device **ufficiale** (non Baileys/protocollo reimplementato — è la differenza che pesa di più), IP residenziale senza proxy, una sola sessione, nessuna iniezione di fingerprint, 20 invii in 14 giorni, destinatari consenzienti;
- contro: l'automazione **resta contro i ToS** a prescindere dall'indistinguibilità, ed esistono vettori di detection browser (`navigator.webdriver`, artefatti CDP) che Patchright neutralizza ma che nessuno ha mai verificato *contro WhatsApp nello specifico*.

Tommaso ha accettato il trade-off il 27/07 dopo averlo avuto per iscritto. La **verifica di detection a vuoto** (aprire WhatsApp Web col profilo Patchright *senza* login e leggere cosa la pagina vede dell'ambiente) è stata proposta e **declinata**: se PoC-1 mostra anomalie — logout ripetuti, re-scan richiesti, schermate insolite — è la prima cosa da riprendere prima di proseguire.

**Cosa NON cambia:** allowlist fail-closed, watcher che non apre le chat, guardia pre-invio, messaggi veri. Cambia solo *quali* numeri stanno in `POC_WA_ALLOWED_NUMBERS`.

**Cosa NON eredita M1:** è una deroga di M0. La produzione gira su numeri di servizio.

---

## 13-bis. Decisioni ed emersi dell'esecuzione (27/07, pomeriggio)

| Q | Decisione | Data | Impatto |
|---|---|---|---|
| — | **PoC-2 si chiude a 13 invii su 20.** Criterio di volume dichiarato mancato, non aggirato | 27/07 | il verdetto su PoC-2 si argomenta sul **tasso di consegna** (13/13) e sui tempi misurati, non sul volume |
| — | **PoC-4 bypassato** per evidenza già raccolta sul campo | 27/07 | vedi sotto: due scenari restano scoperti |
| — | **Revoca dell'opt-out = override manuale di Tommaso**, nessun comando in M0 | 27/07 | evenienza rarissima. Diventa **requisito M1** quando l'opt-out passa a DB, non debito di M0 |
| — | **Il messaggio che chiedeva "rispondi STOP" è stato RIMOSSO da `messages.txt`** | 27/07 | restava sorteggiabile da `poc2_send`: avrebbe messo in opt-out permanente altri contatti veri. Gli indici di `--messaggio-n` sono scalati di 1 |
| — | **`poc4_coexist` passa da deep-link a `open_by_search`** | 27/07 | era rimasto sulla strategia scartata in PoC-2a: avrebbe misurato la coesistenza su codice che M3 non usa |
| — | **PoC-1 dato per passato in anticipo**: la pianificazione di M1 parte senza aspettare il 10/08 | 27/07 | motivo di Tommaso: una sessione caduta si risolve facendo riscansionare il QR al cliente. **Ma** ogni riscansione riapre la finestra di risync (A9) ⇒ la quarta guardia diventa **obbligatoria**, non opzionale. La *frequenza* di caduta va misurata lo stesso: decide se M1 ha bisogno di riavvio automatico e alerting |
| — | **Deployment: il browser gira sul PC di Tommaso.** In futuro possibile anche sui PC dei clienti, con credenziali e dati loro | 27/07 | **niente pagina admin per il QR da remoto in M1**: il login è assistito e locale (calco `manual_login.py`). Vincolo di progetto: sessione/QR **dietro un'interfaccia** e multi-tenant **nello schema fin da subito**, altrimenti il passaggio a "il cliente lo esegue in casa sua" è una riscrittura. Capienza misurata: **1 numero per volta** su questa macchina (1,2 GB per profilo su 7,4 GB) |

### Il criterio "guardia ≤ 2s" va ritarato, non dichiarato fallito

Misurato su 13 invii: `guardia_dom_ms` mediana **5,7 s**, p95 7,5 s, max 12,1 s. **12 invii su 13 sopra soglia.** L'unico sotto (21 ms) è il primo, mandato *prima* che la guardia venisse riscritta.

Non è una regressione: la soglia fu fissata quando si credeva che la coda inbound fosse leggibile senza scroll. La conversazione è virtualizzata, caricare la cronologia costa 2-12 s e **non è aggirabile senza rinunciare alla garanzia opt-out**. La soglia giusta si scrive dai dati, non dall'intenzione.

Costo pieno di un invio: mediana **47 s**, p95 60 s. È da qui che escono i cap giornalieri realistici (Q50), non da `guardia_totale_ms`.

### Perché PoC-4 è stato bypassato, e cosa resta scoperto

**Decisione di Tommaso, argomentata:** durante il batch di invii del 27/07 stava già scrivendo a mano dal telefono sullo stesso numero, senza alcun problema. Il multi-dispositivo è funzione **nativa** di WhatsApp Business: non è lì che si rompe. La sua diagnosi — il rischio non è la concorrenza umano/bot, è il **riconoscimento dell'automazione** — è condivisa, e sposta il lavoro vero sulla simulazione del comportamento umano.

**Copre S1 e S2.** Restano due cose che quell'evidenza non tocca:

- **S3 — finestra TOCTOU della guardia.** Tra la lettura della coda inbound e l'invio effettivo passano ~20 s misurati (typing umano + conferma). Uno STOP che arriva **dentro** quella finestra non viene visto: la guardia ha già deciso. Non serve un test per stabilirlo, è strutturale. Va scritto come **limite noto** e mitigato in M1 (ri-lettura della coda subito prima di premere invio, molto più economica della prima perché la cronologia è già caricata).
- **S4 — inbound già letto dall'umano.** Se il cliente legge una risposta dal telefono, il badge "non letto" sparisce e **il watcher quella risposta non la vede più**. Se conteneva uno STOP, è perso. Non richiede invii: è osservazione pura, e resta l'unico scenario di PoC-4 che varrebbe la pena eseguire.

### RISCHIO NUOVO — la sincronizzazione incompleta rende cieca la guardia opt-out

Sollevato da Tommaso il 27/07. WhatsApp Web **non sincronizza tutte le chat all'istante**: su profili con molte chat (il catalogo DOM ne ha misurate **485**) le conversazioni arrivano progressivamente, e una chat può mostrare solo gli ultimi messaggi mentre i precedenti sono ancora in arrivo. Dopo uno scollegamento e un nuovo collegamento la risincronizzazione riparte da capo.

**Perché è grave.** La guardia promette una cosa sola: *prima di scrivere, controllo se questo mi ha detto STOP*. Su una chat non ancora sincronizzata la guardia **non legge un silenzio: legge il vuoto** — e lo tratta da silenzio. Poi invia.

**Il codice oggi non sa distinguere i due casi.** `carica_cronologia` (`_common.py`) marca `esaurita` dopo **3 giri di scroll senza nuovi messaggi**: una chat finita e una chat non ancora sincronizzata si comportano in modo identico.

**La mitigazione proposta da Tommaso** — "l'operatore aspetta che WhatsApp sincronizzi" — è necessaria ma non sufficiente: è una procedura, e le procedure saltano. La difesa tecnica coerente col resto del sistema è una **quarta guardia fail-closed**: se in pagina c'è l'indicatore di sincronizzazione in corso, **non si invia**, esattamente come già accade quando la coda inbound non viene agganciata. Cecità dichiarata, mai scambiata per silenzio.

**Perché resta aperta.** Catturare il selettore dell'indicatore richiede di scollegare e ricollegare WhatsApp Web, cioè un **re-scan del QR: è l'unica cosa che azzera PoC-1** e butta via i 14 giorni. Non si fa. Si cataloga al **primo re-scan che capiterà comunque** (crash, logout o fine di PoC-1): è un'osservazione gratuita, va solo colta invece che sprecata.

### Il browser è morto da solo dopo 16 minuti — la sessione no

Il daemon persistente è partito alle 12:22 UTC ed è morto entro le 12:38 (`TargetClosedError`), senza che nessuno lo chiudesse. Nessun crash di Chrome nell'Event Log di Windows: **la causa non è provata**. La pressione di RAM è l'ipotesi principale (7,4 GB totali sulla macchina, ~1,2 GB per profilo, 1,9 GB liberi a riposo), ma resta ipotesi.

Due fatti invece stabiliti, entrambi requisiti per M1:
- **la sessione WhatsApp è sopravvissuta**: l'heartbeat successivo ha trovato la lista chat, nessun QR, nessun re-scan. Per il criterio di PoC-1 non è un guasto — è morto il processo, non la sessione;
- **il daemon se n'è accorto solo allo scan successivo**: fino a **15 minuti da morto senza saperlo**. Un watcher di produzione ha bisogno di un liveness check più fitto dello scan e di un riavvio automatico, altrimenti perde inbound in silenzio.

---

## 14. Prossimo passo
SDD **v1.2** (24/07). Piano M0 scritto (24/07) ed **eseguito lato codice** (26/07: task 1-9, script PoC + helper testati, 71 test verdi). Restano: **Task 0 fisico** (numeri in allowlist, 3 messaggi, slot linked device) → **login QR** → i 14 giorni di PoC-1, con gli altri PoC dentro la finestra. Gate duro invariato: PoC-1/2/3 falliti ⇒ strada A rimessa in discussione prima di costruire M1+.
