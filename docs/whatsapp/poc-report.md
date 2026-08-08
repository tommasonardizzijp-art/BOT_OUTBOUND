# PoC report — canale WhatsApp M0

> **STATO: BOZZA.** I numeri qui dentro sono misurati e definitivi per PoC-2, PoC-3a e PoC-4.
> Il **verdetto finale è datato 10/08/2026**, quando si chiude la finestra di 14 giorni di PoC-1.
>
> Periodo: **27/07/2026 → 10/08/2026** · Numero: **personale di Tommaso su WhatsApp Business**
> (deroga di M0 decisa il 27/07, sostituisce il secondario Primero — vedi [`00-problematiche-e-decisioni`](00-problematiche-e-decisioni.md) §13)
> Fonti: `D:\dev\wa-poc\artifacts\` → `heartbeat.csv`, `open_results.csv`, `send_results.csv`, `scan_*.json`, `events.jsonl`

---

## Verdetto

| PoC | Criterio | Misurato | Esito |
|---|---|---|---|
| **PoC-1** sessione | 14gg, ≥5 riavvii browser, ≥2 riavvii PC, nessun re-scan | giorno **0/14**. 2 heartbeat, sessione viva entrambi, **nessun QR mai richiesto** | ⏳ **10/08** |
| **PoC-2** apertura | ≥90% aperture OK su una strategia | **17/17 post-fix (100%)** su `open_by_search`. 2 KO pre-fix, causa nota e corretta | ✅ **GO** |
| **PoC-2** invio | 20/20 su ≥6 chat controllate | **13 invii, 13/13 consegnati**, 7 destinatari distinti | ⚠️ **volume mancato** (13/20), consegna piena |
| **PoC-2** guardia (lettura) | `guardia_dom_ms` ≤ 2s | mediana **5,7 s** · p95 7,5 s · max 12,1 s · **12/13 sopra soglia** | ❌ **criterio da ritarare** (non è un fallimento: vedi sotto) |
| **PoC-2** guardia (costo reale) | misurato e riportato, nessuna soglia | `guardia_totale_ms` mediana **22,2 s** · p95 26,7 s | ℹ️ non è un gate |
| **PoC-2** STOP | invio bloccato dallo STOP in coda | **bloccato sul campo**, opt-out scritto, retry successivo skippato senza aprire la chat | ✅ **GO** |
| **PoC-3** rilevamento | 20/20 inbound entro 1 ciclo, ≥5 spontanei | **in raccolta**. 4 scan eseguiti, 66-68 righe ciascuno | ⏳ **in corso** |
| **PoC-3** non-lettura | nessuna chat marcata letta dal watcher | watcher non apre nessuna chat **per costruzione** (nessun click sulle righe) | ✅ per design, ⬇️ **declassato da gate a rischio residuo** (27/07) |
| **PoC-4** coesistenza | nessun doppio invio, nessun logout | **bypassato** su evidenza di campo | ⏭️ vedi §PoC-4 |

**Decisione operativa presa il 27/07:** PoC-1 si **dà per passato in anticipo** e la pianificazione di M1 parte in parallelo, senza aspettare il 10/08. Motivo di Tommaso: una sessione caduta è risolvibile facendo ri-scansionare il QR al cliente, che ha l'account in mano.

**Il gate resta però parzialmente in piedi, e la ragione è tecnica, non procedurale.** Ogni re-scan fa ripartire la **sincronizzazione**, cioè la finestra in cui la guardia opt-out è cieca (§Rischi). "Basta riconnettersi" e "la sincronizzazione è un problema" non possono valere insieme: se il rimedio alla caduta è la riconnessione, la **quarta guardia sul risync diventa obbligatoria in M1**, non opzionale. E la *frequenza* di caduta va misurata comunque, perché decide se M1 ha bisogno di riavvio automatico e alerting o se è un caso raro.

---

## Numeri che servono a M1-M3

- **Costo pieno di un invio** (apertura + guardia + typing + verifica spunta): mediana **47,2 s**, p95 **59,6 s**, max 61,1 s (n=13). È **questo** il numero da cui escono i cap giornalieri realistici (Q50), non `guardia_totale_ms`.
- **Costo della guardia pre-invio:** mediana **5.728 ms**, p95 **7.494 ms**, max 12.098 ms (Q73).
- **Costo dell'apertura chat:** mediana **17,8 s**, p95 19,8 s.
- **RAM per sessione:** 1.185 MB e 1.135 MB nelle due misure → **~1,2 GB per profilo browser**. Sulla macchina attuale (7,4 GB totali, ~1,9 GB liberi a riposo) significa **una sola sessione comoda, due al limite**. Scalare = server, non questo PC (A5).
- **Virtualizzazione lista:** **sì, severa**. 485 chat dichiarate (`aria-rowcount`), **67-70 renderizzate** (~14%). Lo scan vede solo la cima della lista — accettabile solo perché WhatsApp ordina per attività recente, quindi un inbound nuovo sale in cima da solo. **Non** accettabile per cercare una chat vecchia: quella si raggiunge solo per ricerca.
- **Virtualizzazione conversazione:** sì (`data-virtualized`, 35/35). È la ragione per cui la guardia costa secondi e non millisecondi.
- **Strategia di apertura scelta: `open_by_search`** (ricerca), non il deep-link. Motivo dai dati: il deep-link su un numero **senza** chat ne **crea una nuova**, che viola il vincolo "solo contatti che ci hanno già scritto" (V2).

---

## PoC-2 — invio e guardia

**13 invii reali, 13 consegnati, 0 falliti, 0 code inbound non agganciate.** Spunte lette: 10 `Consegnato`, 3 `Letto`. 7 destinatari distinti.

**Perché 13 e non 20.** Il pool di testi era di 3 messaggi, uno dei quali chiedeva "rispondi STOP" ed è stato **bruciato** dopo il test negativo (usarlo di nuovo avrebbe messo in opt-out permanente altri contatti veri). Con 2 testi utilizzabili e 6 contatti disponibili il tetto matematico era 12+1. Tommaso ha deciso il 27/07 di **chiudere a 13 e dichiarare il criterio mancato**, invece di allargare l'allowlist o scrivere testi nuovi. Il criterio di volume è **mancato**, non aggirato.

**Perché "guardia ≤ 2s" non è un fallimento ma una taratura sbagliata.** La soglia fu fissata quando si credeva che la coda inbound fosse leggibile senza scroll. La conversazione è virtualizzata: senza caricare la cronologia, nel DOM restano ~17 messaggi degli ultimi minuti, e uno STOP di venti minuti prima **non esiste**. Il caricamento è **parte della guardia**, non un accessorio, e costa 2-12 s. L'unica misura sotto soglia (21 ms) è il primo invio, fatto *prima* della riscrittura: era veloce perché non guardava niente. **La soglia va riscritta dai dati: `guardia_dom_ms` ≤ 10 s, p95 ≤ 8 s.**

**Test negativo dello STOP — passato sul campo.** Su un contatto reale che aveva scritto STOP: invio **annullato**, opt-out persistito, e al tentativo successivo il numero è stato **skippato senza nemmeno aprire la chat**. È la garanzia più importante del sistema ed è l'unica verificata contro la realtà e non contro un mock.

**Apertura chat.** 2 fallimenti alle 11:18, entrambi con lo stesso segnale (`nessuna-sezione-chat`): la casella di ricerca non veniva svuotata e il secondo numero si accodava al primo. Corretto (`svuota_ricerca`), e da lì **17 aperture su 17** riuscite. Il tasso grezzo 4/6 non è rappresentativo di nulla se non del bug.

---

## PoC-3 — rilevamento dalla sola lista

Lo scanner funziona e non apre nessuna chat. 4 scan: 68, 66, 68, 66 righe.

**I 20 inbound non sono ancora raccolti** e c'è un problema di metodo da dichiarare: lo scan misura i **non letti in quel momento**, non gli inbound arrivati. Tra i due scan delle 13:12 e delle 14:21 i messaggi non letti sono passati da 27 a 4 — non perché non siano arrivati, ma perché **Tommaso li ha letti dal telefono**. La prova che il contatore va ricostruito per **delta tra scan**, con una verità di riferimento su cosa è arrivato davvero, è già nei dati.

Ed è anche la **dimostrazione diretta dello scenario S4** che PoC-4 avrebbe dovuto testare: un inbound letto dall'umano **sparisce dalla vista del watcher**. Se dentro c'era uno STOP, l'abbiamo perso. Non è più un'ipotesi: è successo oggi, quattro volte, nei nostri stessi artefatti.

**Limiti misurati dello scan** (dal catalogo DOM, non nascosti):
- **46 anteprime vuote su 68** (68%). L'anteprima serve a individuare candidati, non è mai stata la garanzia di opt-out — ma significa che **uno STOP visibile nella sola anteprima, per la maggior parte delle righe, non lo vediamo**.
- **8 chat su 68 hanno il titolo numerico** (contatto non in rubrica). `title_is_number` lo distingue: è il segnale che in M1 decide se un `chat_title` è PII da mascherare.
- Direzione dell'ultimo messaggio: presenza di `wds-ic-*` ⇒ il messaggio è nostro (62/68 senza icona = ultimo messaggio dell'altro).

---

## PoC-4 — bypassato, e cosa resta scoperto

**Decisione di Tommaso del 27/07, su evidenza di campo:** durante il batch di invii stava già scrivendo a mano dal telefono sullo stesso numero, senza alcun problema. Il multi-dispositivo è funzione **nativa** di WhatsApp Business. La sua diagnosi — il rischio non è la convivenza umano/bot, è il **riconoscimento dell'automazione** — è condivisa e sposta il lavoro vero sulla simulazione del comportamento umano.

Copre S1 e S2. Restano scoperti:
- **S3 — finestra TOCTOU.** Tra lettura della coda e invio passano ~20 s misurati. Uno STOP che arriva **dentro** quella finestra non viene visto. Strutturale, non serve testarlo. Mitigazione M1: **ri-leggere la coda subito prima di premere invio** (costa poco, la cronologia è già caricata).
- **S4 — inbound già letto dall'umano.** Già osservato nei dati di PoC-3 (sopra). Non richiede invii ed è l'unico scenario che varrebbe ancora la pena eseguire.

---

## Cosa si è rotto e come si è presentato

| Cosa | Come si è presentato | Stato |
|---|---|---|
| `div.message-in` / `div.message-out` | **0 nodi agganciati** su una chat con 35 messaggi. La guardia sarebbe tornata `null` a ogni invio: fail-safe ma inutilizzabile | risolto: direzione da 3 segnali combinati asimmetricamente |
| Coda inbound letta senza caricare la cronologia | Nel DOM solo ~17 messaggi degli ultimi 3 minuti. Uno STOP di 20 minuti prima **non esisteva** | risolto: `carica_cronologia` dentro la guardia |
| Guardia ferma al primo messaggio nostro | Uno STOP seguito da una nostra risposta diventava invisibile per sempre | risolto: legge gli ultimi 40 inbound ovunque siano |
| Ricerca non svuotata tra due numeri | Il secondo numero si accodava al primo → `nessuna-sezione-chat` | risolto: `svuota_ricerca` con verifica |
| Selezione risultati con Enter / per posizione | Le intestazioni di sezione sono `[role='row']` come le chat; sotto ci sono i **gruppi** | risolto: `apri_chat_da_risultati()` |
| Deep-link per aprire le chat | Su un numero senza chat **ne crea una nuova** (viola V2) | risolto: `open_by_search` ovunque, incluso `poc4_coexist` |
| `U+202A`/`U+202C` nei `title` | `UnicodeEncodeError` su console cp1252: **script morto** | risolto: rimozione nello scanner + stdout `errors='replace'` |
| `msg-container` e `[data-id]` annidati | Lista dei messaggi raddoppiata | risolto |
| Spunte cercate come `data-icon status-*` | Non esistono: sono `aria-label` `Consegnato`/`Letto`, **localizzati in italiano** | risolto, ma **fragile al cambio lingua** → da irrobustire in M1 |
| Browser del daemon morto dopo 16 minuti | `TargetClosedError` allo scan successivo. Nessun crash nell'Event Log | **causa non provata** (RAM è ipotesi). Sessione WhatsApp sopravvissuta |

---

## Rischi aperti che M1 deve affrontare

1. **Sincronizzazione incompleta = guardia cieca.** WhatsApp Web non sincronizza tutte le chat subito (485 su questo profilo) e riparte da capo dopo una riconnessione. Su una chat non ancora sincronizzata la guardia **non legge un silenzio: legge il vuoto**, e lo tratta da silenzio. `carica_cronologia` non sa distinguere "chat finita" da "chat non ancora arrivata": entrambe smettono di produrre messaggi. **Rimedio: quarta guardia fail-closed** — indicatore di sincronizzazione presente ⇒ non si invia. Il selettore **non è ancora catalogato** perché catturarlo richiede un re-scan del QR, cioè l'unica cosa che azzera PoC-1: si cattura al primo re-scan che capiterà comunque.
2. **Finestra TOCTOU della guardia** (~20 s). Vedi PoC-4/S3.
3. **Inbound letto dall'umano sparisce dal watcher.** Vedi PoC-3/S4. Già osservato, non ipotetico.
4. **STOP detto a voce non viene sentito.** I vocali oggi non si leggono: la garanzia opt-out vale **solo per gli STOP scritti**. Va dichiarato al cliente, non lasciato implicito. Prima mossa in fase 2: verificare la **trascrizione nativa** di WhatsApp prima di pagare uno speech-to-text (vedi [`sviluppi-futuri`](sviluppi-futuri.md) F9).
5. **Il daemon non sa di essere morto** fino allo scan successivo (15 min). Serve liveness check più fitto dello scan + riavvio automatico.
6. **Selettori localizzati in italiano** (`Consegnato`/`Letto`, `Tu:`). Un cliente con interfaccia in altra lingua rompe la lettura delle spunte e un segnale di direzione su tre.
7. **Layer proxy mai validato** (Q98, nessun proxy in M0). Va provato prima di qualunque campagna pagante.
8. **Ban sul numero personale di Tommaso.** Deroga di M0: la produzione gira su numeri di servizio. 13 invii in un giorno a contatti consenzienti, nessuno stress test.

---

## Domande §17 chiuse da M0

| Q | Risposta misurata |
|---|---|
| **Q19** | Titolo chat: nome di rubrica, **numero** se il contatto non è in rubrica (8/68). `title_is_number` lo distingue → serve a decidere cosa è PII |
| **Q39** | Le spunte si leggono: `aria-label` `Consegnato`/`Letto` (**non** `data-icon status-*`), localizzate |
| **Q40** | Lista **virtualizzata**: 485 dichiarate, 67-70 nel DOM (~14%) |
| **Q41-Q42** | Struttura riga e segnali chiusi nel catalogo DOM |
| **Q73** | Costo della guardia: mediana 5,7 s, p95 7,5 s |
| **Q50** (parziale) | Costo pieno di un invio: mediana 47 s → base per i cap giornalieri |

---

## Domande ancora aperte, col motivo

- **Selettore dell'indicatore di sincronizzazione** — catturarlo richiede un re-scan del QR, che azzererebbe PoC-1. Si cattura alla prima riconnessione che capita comunque.
- **Schermata QR** — mai osservata: la sessione fu stabilita a mano prima che gli script guardassero. Stesso momento di cattura.
- **Causa della morte del browser** — nessun crash loggato; RAM è ipotesi non provata. Serve un secondo episodio con la memoria monitorata.
- **Perché 46 anteprime su 68 sono vuote** — media, vocali, eventi di sistema o virtualizzazione: non distinto.
- **PoC-3b: 20 inbound con ≥5 spontanei** — in raccolta. Richiede anche di ricostruire il conteggio per **delta tra scan**, perché i non letti spariscono quando l'umano legge dal telefono.
- **Tempo di uno scan completo delle 485 chat** — mai misurato: gli scan attuali leggono solo la finestra renderizzata. Se costa minuti, il watcher di M1 va ripensato.
- **Frequenza di caduta della sessione** — è il dato per cui PoC-1 esiste. Dato per passato in anticipo, ma il numero serve lo stesso a decidere riavvio automatico e alerting.
