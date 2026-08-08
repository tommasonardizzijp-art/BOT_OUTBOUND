# Canale WhatsApp — Sviluppi Futuri (backlog fase 2+)

> Stato: **backlog** · Data: 2026-07-23
> Cose CONSAPEVOLMENTE rimandate dopo l'MVP, per non perderle. Ogni voce: cosa, perché rimandata, quando riprenderla.
> Riferimenti: [[00-problematiche-e-decisioni]]. Voce gemella in `docs/project/PROGRESS.md`.

---

## Perimetro MVP (fase 1) — per contrasto

L'MVP include SOLO: invio sequenze semplici (lista messaggi `msg1→2→3`) con **branching base** (ha risposto / non ha risposto / attendi X giorni), rilevamento risposte via DOM (per branching + statistiche), template fissi A/B/C/D + spintax + placeholder da CSV, ingest lista CSV, cap invio basso modificabile a mano, multi-tenant lato admin. Tutto il resto è qui sotto.

---

## Backlog fase 2+

### F1 — Flow builder visuale (modificabile, multi-flow)
- **Cosa:** editor visuale stile n8n/GoHighLevel per costruire flow di campagna arbitrari, con **tanti flow modificabili per (tenant, campagna)**. Due strade: (a) integrare **n8n** come motore di flow (n8n = cervello visuale, BOT OUTBOUND = esecuzione+anti-detect, dialogo via webhook); (b) build-own con React Flow + tabella `flow_definition` (JSON).
- **Perché rimandata:** l'MVP serve in tempi brevi; un builder visuale è settimane/mesi di lavoro. Le sequenze semplici coprono i primi casi.
- **Come riprenderla:** l'MVP deve esporre **invio e risposta come interfacce webhook-ready** → così n8n (strada a) si innesta quasi gratis. Riprendere quando serve flessibilità per un cliente reale.
- **Preferenza Tommaso:** n8n visivamente il top; GoHighLevel buon riferimento per il branching.

### F2 — UI cliente self-serve
- **Cosa:** interfaccia semplificata con cui il cliente configura/monitora da solo le proprie campagne (vista per-ruolo accanto alla vista admin).
- **Perché rimandata:** v1 = solo admin (Tommaso opera tutto). Managed-service prima, self-serve dopo.
- **Come riprenderla:** riusare `roles.py`; la vista cliente è un sottoinsieme filtrato della dashboard admin per `tenant`.

### F3 — AI lettura-conversazione (2 modalità)
- **Cosa:** (i) **follow-up AI**: prende i contatti che non rispondono da X giorni, **legge gli ultimi ~10 messaggi** della chat (scorrendo il thread), genera un follow-up personalizzato coerente con ciò che si sono detti; (ii) **hook personalizzato**: rilegge la chat e aggancia un dettaglio reale nel messaggio campagna. Richiede una **skill/prompt dedicata** ben tarata (plausibile, no allucinazioni).
- **Perché rimandata:** fase 2. MVP parte con template fissi + placeholder da CSV (come il bot IG).
- **Come riprenderla:** riusa `ai_personalizer.py` (multi-provider + failover) con nuovo input = contesto conversazione; il `WhatsAppWebPage` deve saper estrarre la cronologia (ultimi 10). **Vincolo GDPR (P3):** mandare testo conversazione a un provider AI = PII a terzo → base giuridica + DPA + no provider non-UE su PII (lezione TheVista).

### F4 — Auto-reply con timer anti-doppio-messaggio (coesistenza fase 2)
- **Cosa:** il bot può rispondere agli inbound entro N minuti (timer randomizzato); oltre, gestisce l'umano. Con **lock** che prima di inviare verifica se l'umano ha appena scritto dal telefono → evita il doppio messaggio.
- **Perché rimandata:** all'MVP la coesistenza è "umano-prima" (il cliente vede la risposta dalle notifiche di WhatsApp Business e gestisce). Auto-reply introduce race condition da gestire con cura.
- **Come riprenderla:** lock a livello (numero, contatto) + controllo "ultimo messaggio in uscita umano" via DOM prima dell'invio bot.

### F5 — Ingest via API CRM (oltre il CSV)
- **Cosa:** integrazione diretta con CRM esterni via API (es. endpoint dedicato su **Primero**), invece del solo export CSV.
- **Perché rimandata:** Primero non espone ancora API utili; il CSV copre l'MVP e tutti i CRM.
- **Come riprenderla:** definire un contratto ingest generico (lista numeri + campi arbitrari) con due adattatori: CSV (MVP) e API (fase 2).

### F6 — Statistiche/analytics avanzate per campagna
- **Cosa:** dashboard analytics oltre le metriche base (tassi risposta, conversione, drop-off per step del flow).
- **Perché rimandata:** MVP registra le risposte (via DOM) per stat base come su IG; l'analytics ricca viene dopo il flow builder.

### F7 — Multi-numero esteso
- **Cosa:** oltre il limite MVP (max 2 numeri per IP/proxy, stessa azienda), gestione scalata di molti numeri/proxy.
- **Perché rimandata:** ~10 clienti nei primi 6 mesi; il modello 1 proxy mobile ↔ max 2 numeri stessa azienda basta.

### F8 — Profilazione del contatto dal contenuto della conversazione
- **Cosa:** leggere la conversazione già avvenuta e ricavarne un **profilo del contatto** (preferenze, interessi, categorie di prodotto/servizio) su una tassonomia **decisa dal cliente** per il suo settore. Serve a segmentare le campagne, non solo a personalizzare il singolo messaggio (che è F3).
- **Dove vivono le chat — risposta alla domanda del 27/07.** WhatsApp Web tiene le sue chat in **IndexedDB dentro il profilo del browser**, in forma gestita e offuscata dall'app. **Non si legge quel database.** Due motivi: si rompe a ogni aggiornamento dell'app, ed è concettualmente la stessa cosa della **strada B già scartata** (parlare col protocollo/dati di WhatsApp invece che con l'interfaccia). La strada giusta è l'opposta: **un nostro DB**, popolato da ciò che estraiamo dal DOM come farebbe una persona che legge. L'estrazione della cronologia serve già a F3: F8 ci si appoggia, non apre un fronte nuovo.
- **Perché rimandata:** l'MVP non legge le conversazioni. Prima deve esistere l'estrazione della cronologia (F3), poi la persistenza, poi la profilazione.
- **Come riprenderla:** (1) tabella `conversazione_messaggio` (tenant, contatto, direzione, testo, ts) popolata dal DOM; (2) passata LLM per contatto con tassonomia per-tenant come input; (3) il profilo diventa un filtro di targeting per le campagne.
- **⚠️ Vincolo GDPR, più pesante di F3.** Qui non si manda un messaggio: si **profila una persona** sulla base di conversazioni private. È trattamento ulteriore rispetto alla finalità per cui la chat esisteva, richiede base giuridica propria e informativa, e ricade nel territorio dell'art. 22 (decisioni automatizzate/profilazione). **Va messo esplicitamente sul tavolo del parere legale già in corso**, non trattato come dettaglio implementativo. Vale anche il vincolo di F3: testo di conversazione a un provider AI = PII a un terzo → DPA e nessun provider fuori UE su PII (lezione TheVista).

### F9 — Note vocali: trascrizione e comprensione
- **Cosa:** molti clienti rispondono con **audio**. Oggi per noi un vocale è un buco: il watcher vede che è arrivato qualcosa e non sa cosa dice. Serve trascriverlo e trattarlo come testo — inclusa la cosa più importante: **uno STOP detto a voce oggi non lo intercettiamo**.
- **Prima di costruire, la cosa gratis:** WhatsApp ha una **trascrizione nativa** delle note vocali. Se è attiva sull'account, il testo è già in pagina e si legge dal DOM come qualunque altro messaggio — zero costo, zero provider, zero PII che esce. **Va catalogato come prima cosa** quando si apre questo fronte: cambia completamente il costo della feature.
- **Se la nativa non basta:** scaricare l'audio in-page e passarlo a uno speech-to-text. Non gira su questa macchina (7,4 GB di RAM, già al limite con un solo profilo browser): è lavoro lato server.
- **Perché rimandata:** fase 2+, e dipende da F3 per l'estrazione della cronologia.
- **⚠️ GDPR:** un vocale mandato a un provider STT è contenuto personale che esce verso un terzo, esattamente come F3/F8. Stesso vincolo: base giuridica, DPA, no provider extra-UE su PII.
- **Nota di sicurezza (opt-out):** finché i vocali non si leggono, la garanzia opt-out vale **solo per gli STOP scritti**. È un limite da dichiarare al cliente, non da lasciare implicito.

---

## Regola
Quando una di queste entra in sviluppo: spostare la voce in un design doc dedicato (`docs/whatsapp/`) + spec/plan, e loggare in `PROGRESS.md`.
