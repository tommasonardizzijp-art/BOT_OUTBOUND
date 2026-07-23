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

---

## Regola
Quando una di queste entra in sviluppo: spostare la voce in un design doc dedicato (`docs/whatsapp/`) + spec/plan, e loggare in `PROGRESS.md`.
