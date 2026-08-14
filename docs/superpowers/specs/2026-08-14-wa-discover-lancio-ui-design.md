# Lanciare l'auto-discover WhatsApp dalla UI — design

Data: 2026-08-14
Stato: approvato in brainstorming, da tradurre in piano esecutivo
Branch: `feat/wa-discover-lancio` (worktree `.worktrees/wa-discover-lancio`, da `origin/main` @ `94de751`)

## 1. Il problema

Il motore della Fase A auto-discover è completo, mergiato su `main` e collaudato
contro il DOM vero: `esegui_discover_run(number_id, *, soglia_sync, headless)` in
`backend/app/services/wa_discover_run.py:292`, con il pacchetto
`backend/app/services/wa_discover/` e la tabella `wa_discovered_chats`
(migrazione `032`). La Fase B — promozione a `WaContact`, arruolamento in
campagna, pagina `/wa/scoperti` — è anch'essa completa.

Manca una cosa sola: **non esiste nessun modo di lanciare uno scan che non sia
un file Python**. `esegui_discover_run` è chiamato solo da
`backend/scripts/poc_wa/collaudo_wa_discover.py`, che ha il `number_id` cablato
alla riga 32 e un `sys.path.insert` verso un worktree alla riga 26. Nessun
endpoint HTTP lo espone, nessun job ARQ lo accoda, nessun cron lo registra.

Non è una dimenticanza: il piano della Fase A dichiarava la UI fuori scope
("la Fase B avrà bisogno di una lista approvabile: si disegna lì, non qui") e la
Fase B ha disegnato la lista di approvazione ma non il lancio. Il comando è
rimasto nel mezzo.

### Cosa serve, in concreto

Il 14/08/2026 è stato collegato il numero `PRIMERO MAGAZZINO`
(`e4b020cc-f906-4dbe-981a-27a4973c253f`) e creata la campagna
`PRIMERO MAGAZZINO SCORTA AGOSTO` (`249ece3a…`), vuota, in attesa di
destinatari. Per riempirla servono i contatti di quel numero, cioè uno scan —
che oggi si fa solo a mano, da riga di comando, sapendo dove guardare.

## 2. Decisioni prese

| Decisione | Scelta | Motivo |
|---|---|---|
| Dove vive il comando | Azione **sul numero**, in `/wa/numeri` | La rubrica è un fatto del numero, indipendente da quante campagne ci si fanno sopra |
| Sezione nuova nella nav | **No** | `/wa/scoperti` esiste già ed è metà di quella pagina: le manca lancio, esito e storico. Due pagine sulle stesse righe sarebbero una di troppo |
| Esecuzione | **Job ARQ** dedicato | Il browser vive nel worker come tutti gli altri browser del progetto. `BackgroundTasks` scartato: su Windows `uvicorn --reload` spegne Playwright e ogni riavvio perde lo scan a metà lasciando il lock. SSE sincrono scartato: la richiesta resterebbe appesa per i minuti che lo scan dura |
| Se il browser è occupato | **Rifiuta e spiega** (409) | Nessuna coda, nessuno stato differito: o parte adesso o dice perché no. Un browser che si apre da solo mezz'ora dopo, quando nessuno guarda, è peggio di un rifiuto |
| Memoria degli scan | Tabella **`wa_discover_runs`** | Serve a rispondere a "perché stavolta ne ha trovati 12" senza aprire i log. Col periodico diventa indispensabile: lì nessuno guarda lo schermo |
| Riscansione | **Incrementale**, in questo cantiere | Senza, ogni riscansione ripaga ~12s per chat già vista: su 900 chat sono ore, e il periodico giornaliero è impraticabile |
| Periodico automatico | **Cantiere 2**, dentro il reply-watcher, una volta al giorno | Qui si lascia solo la presa (`avviato_da`) |

### Perché il periodico va dentro il reply-watcher

`wa_reply_watcher.scan_number` (`backend/app/services/wa_reply_watcher.py:335`)
gira come cron ai minuti :15 e :45 dalle 9 alle 19
(`backend/app/workers/cron_worker.py:307`), con `headless=True` cablato alla
riga 362, e prende lui il lock del profilo.

Legge **lo stesso identico DOM** del discover: stesso pane `#pane-side`, stessi
selettori di riga e di titolo. Il commento in
`backend/app/services/wa_discover/sidebar.py:101` lo dichiara: "riusa la stessa
logica già scritta per `scan_chat_list`". L'unica differenza è che il watcher
**non scorre**: legge la prima schermata, ~67 righe su 485 dichiarate.

Quindi il costo grosso — aprire il browser, caricare la sessione, attendere la
sincronizzazione — il watcher lo paga già 22 volte al giorno. Il passo profondo
in più è solo "invece della prima schermata, scorri fino in fondo". Metterlo lì
non è far partire due cose insieme: è **smettere di aprire un secondo browser**.

Va però strozzato a **una volta al giorno per numero**, e solo quando su quel
numero non c'è una campagna che sta inviando. Uno scan dura minuti; farlo a ogni
giro terrebbe il lucchetto del profilo dalle :15 alle :25, addosso al
`wa_campaign_supervisor` (:10/25/40/55), 22 volte al giorno, per raccogliere
dal secondo giro in poi quasi sempre zero.

## 3. Architettura

```
CANTIERE 1 — il comando manuale
───────────────────────────────
UI /wa/numeri ─[Scansiona contatti]─► POST /api/wa/numbers/{id}/discover
                                        │
                  guardie fail-closed, in quest'ordine:
                    1. numero esiste, tenant dal numero mai dal client → 404
                    2. status == active                       → 409 numero_non_attivo
                    3. kill-switch di canale abbassato        → 409 canale_fermo
                    4. nessun wa:profile-lock:* di NESSUN numero → 409 browser_occupato
                    5. nessuna run 'running' su questo numero → 409 scan_gia_in_corso
                    6. RAM libera >= soglia                   → 409 ram_insufficiente
                                        │
                        crea wa_discover_runs(running, avviato_da='manuale')
                        enqueue ARQ  _job_id = f"wa:discover:{run_id}"
                                        │
                                     worker ARQ
                                        │
                          esegui_discover_run(number_id, headless=True)
                                        │
                        chiude la run: done|failed + contatori + motivo

GET /api/wa/numbers/{id}/discover → ultima run + storico

CANTIERE 2 (piano separato) — il periodico
──────────────────────────────────────────
wa_reply_watcher, cron :15/:45 9-19, headless
   giro normale (prima schermata)      ← sempre
 + passo profondo (scroll fino in fondo)
     se ultimo discover > 24h E nessuna campagna sta inviando
     → riusa il browser GIÀ APERTO, avviato_da='cron'
```

### Le tre scelte non ovvie dello schema

**La guardia 4 è il gate globale che oggi non esiste.** Rifiuta se c'è un lock
di profilo di *qualunque* numero, non solo del suo. I lock sono per-numero, e su
numeri diversi non si escludono: due browser insieme sono 2,4 GB su una macchina
che ne ha 7,5 con Chrome dell'utente sopra. È successo davvero il 14/08: il
sender ha ripreso una mini-sessione mentre uno scan girava sull'altro numero.
La guardia costa una lettura di chiavi Redis e **non richiede di toccare il
sender** — è lui che il lock lo prende già.

**`_job_id` legato al `run_id`, non al `number_id`.** `enqueue_wa_workers` usa
`wa:send:{number_id}` deterministico, e ARQ **scarta in silenzio** un enqueue
duplicato: `accodati:0`, nessun errore. Col `run_id` dentro l'id, ogni scansione
è un job distinto e quel fallimento muto non può ripetersi.

**Il motore non si tocca, tranne dove è rotto.** `esegui_discover_run` resta
com'è. Le due eccezioni sono il salto delle chat note (§5) e il gate di
sincronizzazione (§6), che è un difetto aperto.

## 4. Dati

Tabella nuova, migrazione **`035`** (l'ultima su `main` è
`034_wa_messages_unique_step`).

```
wa_discover_runs
──────────────────────────────────────────────────────────────
id               String36 PK
tenant_id        FK tenants.id        ← risolto dal numero, mai dal client
number_id        FK wa_numbers.id
started_at       timestamptz
finished_at      timestamptz NULL     ← NULL finché gira
stato            running | done | failed
avviato_da       manuale | cron

salvate          int
aggiornate       int
saltate_gia_note int                  ← righe che l'incrementale non ha ricliccato
non_verificate   int
dichiarato       int NULL             ← aria-rowcount
copertura        numeric NULL
motivo           String30             ← i 10 valori del motore
sync_letta       int NULL
sync_stato       letta | assente | ignota
errore           Text NULL

INDEX (number_id, started_at DESC)
UNIQUE parziale (number_id) WHERE stato = 'running'
```

`saltate_gia_note` è la misura che dice se l'incrementale funziona: al secondo
giro dev'essere alta e la durata bassa.

`sync_stato` separa i tre significati che oggi collassano su `None`:
`letta` (percentuale nota), `assente` (Impostazioni si apre, nessuna percentuale
→ sincronizzato), `ignota` (Impostazioni non si trova → non lo sappiamo).

L'unique parziale su `(number_id) WHERE stato='running'` è la guardia 5 scritta
nel DB oltre che nel codice: due click ravvicinati non generano due run.

## 5. La riscansione incrementale

Il costo di uno scan non è nello scorrere: è nel **cliccare ogni riga** per
aprire il pannello informazioni e leggere il numero. Misura del 14/08 su
`PRIMERO MAGAZZINO`: 16 minuti per 78 chat, ~12 secondi l'una.

`_decidi_riga` (`wa_discover_run.py:90`) già oggi evita il pannello quando il
titolo **è** il numero (56% dei casi su Primero, misurato l'11/08). Si aggiunge
un secondo motivo di salto, nello stesso punto e con la stessa forma:

```
per ogni riga, dall'alto:
   titolo già in wa_discovered_chats con phone_hmac  →  salta, costo ~0
   titolo che è già il numero                        →  estrai, costo ~0   (esistente)
   altrimenti                                        →  apri il pannello, ~12s
```

Copre la regola "sotto la data dell'ultimo scan c'è già tutto" senza dipendere
dalle date: nessun selettore nuovo da catturare, nessun formato locale da
interpretare (`ieri`, `12:29`, `13/08/2026`). La fermata anticipata dello scroll
sulla data resta possibile come ottimizzazione successiva.

**Conseguenza dichiarata:** una riscansione non riverifica i numeri già letti.
Se un contatto cambia nome in rubrica il titolo cambia e lo rivediamo; se cambia
numero mantenendo il nome, no. Per lo scopo — trovare chi ci ha scritto di nuovo
— è accettabile. Se servisse la riverifica si aggiunge un flag "riscansione
profonda" e si paga il prezzo pieno.

**Prerequisito:** la regola vale solo dopo **una baseline completa**. Su
`PRIMERO MAGAZZINO` la baseline non c'è (78 su 900 dichiarate).

## 6. Il gate di sincronizzazione è rotto

`leggi_percentuale` (`backend/app/services/wa_discover/sincronizzazione.py:116`)
cerca `[aria-label='Impostazioni'], [aria-label='Settings']`. Il 14/08, su
`PRIMERO MAGAZZINO`, a browser stabile e sessione `logged_in`, quel selettore
**non ha trovato niente** — due volte, in due sessioni distinte.

Il gate restituisce quindi sempre `None`, e `puo_scansionare` tratta `None` come
"non lo so, procedi". Non è un fail-open prudente: è un gate **che non ha mai
funzionato**. Lo scan del 14/08 è partito su un profilo che stava ancora
scaricando la cronologia (60 MB prima, 153 MB dopo; uno sincronizzato ne pesa
~500) e ha raccolto 78 righe su 900 dichiarate, chiudendo `fermato_dopo_stallo`:
era arrivato al fondo di ciò che esisteva localmente in quel momento.

Il lavoro da fare, in ordine:

1. **Ricatturare il selettore** di Impostazioni dal DOM vero e aggiornare
   `docs/whatsapp/wa-dom-catalog.md`.
2. **Separare i tre `None`** in `letta` / `assente` / `ignota`.
3. Su `ignota`: **attendere e ritentare** fino a un tetto, poi chiudere la run
   con quel motivo invece di procedere alla cieca.

Fatto extra da registrare nel catalogo DOM: **WhatsApp sincronizza solo mentre
il browser è aperto**. Un numero appena collegato ha bisogno di una finestra di
sincronizzazione — browser aperto e fermo — prima che un discover abbia senso.

## 7. UI

Nessuna voce di nav nuova. Due punti di contatto sulle pagine esistenti.

### `/wa/numeri` — il comando canonico

```
│ PRIMERO     │ active │ Ultimo scan          │ [QR] [Verifica sessione]  │
│ MAGAZZINO   │        │ 14/08 14:00          │ [Scansiona contatti]      │
│             │        │ 78/900 (9%) stallo ⚠ │ [Modifica warmup]         │
```

Si clona il pattern già in produzione della sessione organica Instagram
(`frontend/app/accounts/page.tsx:578`), che ha già risolto ogni problema di
questa forma:

- `useSWR` con `refreshInterval` **funzione dell'ultimo dato**: 10s finché lo
  stato non è terminale, poi 0. Fail-closed: nel dubbio continua a pollare.
- Mentre gira: bottone disabilitato, spinner, "Scansione in corso — N raccolte".
- Toast **una sola volta** sulla transizione in-corso → finito, via
  `wasActiveRef`. Tre esiti distinti: completato, copertura bassa, fallito.
- Conferma prima di partire, col testo che dice la verità: *apre un browser
  sulla macchina del backend, blocca gli invii su tutti i numeri finché non
  finisce, può durare parecchi minuti*.
- Ogni 409 ha la sua frase, non un errore generico. `browser_occupato` → "un
  altro numero sta usando il browser, riprova fra qualche minuto";
  `ram_insufficiente` → "memoria insufficiente: chiudi qualche finestra".

### `/wa/scoperti` — testata ed esito

```
┌───────────────────────────────────────────────────────────────┐
│ PRIMERO MAGAZZINO                             [Riscansiona]   │
│ ultimo scan 14/08 14:00 · 78 su 900 (9%) · fermato_dopo_stallo│
│ ⚠ sincronizzazione ignota — è il primo indiziato               │
│ 69 promuovibili · 5 gruppi · 4 ignoti           [storico ▾]   │
└───────────────────────────────────────────────────────────────┘
```

### Client

`frontend/lib/waApi.ts`: due funzioni nuove sotto `numeri` —
`discover(numberId)` e `discoverStato(numberId)` — più i tipi `WaDiscoverRun` e
`WaDiscoverStato`.

## 8. Invarianti e collaudo

**L'invariante H va misurata per-numero.** Il collaudo del 14/08 contava
`wa_messages` su tutto il DB ed è diventato rosso per 8 messaggi di un'altra
campagna su un altro numero: un rosso legittimo che, ripetendosi, smette di
essere letto.

**G7 resta, con la formula corretta per l'incrementale:**
`copertura = (salvate + aggiornate + saltate_gia_note) / dichiarato`. Senza
contare i salti, ogni riscansione riuscita sembrerebbe una raccolta al 2%.

Test, uno per comportamento distinto:

- una per ciascuna delle sei guardie dell'endpoint (i sei 409/404);
- unicità parziale sotto due richieste concorrenti (`asyncio.gather` su sessioni
  DB indipendenti, non chiamate sequenziali);
- salto delle chat note in `_decidi_riga`: nota con numero → salta; nota senza
  numero → apre; sconosciuta → apre;
- tri-stato del gate sync: `letta` / `assente` / `ignota`, e su `ignota` la run
  si chiude senza scansionare;
- chiusura della run: su successo, su eccezione del motore, su worker morto
  (run `running` orfana oltre il TTL);
- conteggio di copertura con i salti inclusi.

## 9. Fuori scope

- Il periodico automatico dentro il reply-watcher (cantiere 2, piano separato).
- La fermata anticipata dello scroll sulla data dell'ultimo scan.
- L'azione "scarta" sulle chat scoperte (già fuori scope dalla Fase B).
- Il frequency cap fra campagne: `last_contacted_at` è scritto e mai letto, e i
  tre numeri condividono il tenant `c1f0438d…`, quindi promuovere da MAGAZZINO
  può rimettere in lista chi PRIMERO TEST ha già contattato. È un rischio di
  prodotto reale, ma è di un altro cantiere.
- La riverifica dei numeri già letti.
