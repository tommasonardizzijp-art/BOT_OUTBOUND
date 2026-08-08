# Catalogo DOM WhatsApp Web — rilevato in M0

> **PARZIALE.** Compilato il 27/07 da `probe_selettori.py` su sessione autenticata
> (WhatsApp **Business** web, IT). La **sidebar è verificata**; il **pannello
> conversazione NO** — nessuna chat era aperta, quindi quei nodi non esistevano
> nel DOM e il probe non poteva dire nulla su di loro. Le celle marcate
> `NON VERIFICATO` sono ipotesi, non rilevazioni: vanno riempite dallo Step 1b
> aprendo **una** chat in allowlist. Fino ad allora la guardia pre-invio del
> Task 8 **non ha basi misurate** e nessun invio è lecito.

> Data rilevazione: **2026-07-27** · Lingua interfaccia: **it** · App: **WhatsApp Business** web
> Dump grezzo: `D:\dev\wa-poc\artifacts\probe_selettori.json`

## Sidebar / lista chat — VERIFICATO

| Elemento | Selettore primario | Fallback | Note/robustezza |
|---|---|---|---|
| Pannello lista chat | `#pane-side` (1) | `[data-testid='chat-list']` (1), `[role='grid']` (1) | tre selettori indipendenti agganciano lo stesso pannello: robusto |
| Riga chat | `[role='row']` (67-70) | `div[data-testid='cell-frame-container']` (stesso conteggio) | `[role='listitem']` **non esiste**: era un candidato sbagliato |
| Cella dentro la riga | `[role='gridcell']` (201) | — | ~3 gridcell per riga |
| Titolo chat (nome o numero) | `[data-testid='cell-frame-title'] span[title]` | `span[dir='auto']` | l'attributo `title` ha il nome **intero**; l'innerText è troncato dal CSS |
| Badge non letti | `[data-testid='icon-unread-count']` | testo riga `"N messaggi non letti"` | **Q41 RISOLTA** |
| Preview ultimo messaggio | `[data-testid='last-msg-status']` | | l'attributo `title` ha l'anteprima **intera**, non troncata |
| Direzione ultimo messaggio (in/out) | `<svg><title>wds-ic-*` | | **Q42 RISOLTA** — vedi sotto |
| Chat silenziata | `[data-testid='mute-notifications-refreshed']` | | |
| Chat con se stessi | `[data-testid='message-yourself-row']` | | riga speciale: **non** ha `cell-frame-container` |
| Casella di ricerca | `[role='textbox']` (1) | | `[data-testid='chat-list-search']` **non esiste**; `div[contenteditable='true']` **non aggancia** (0) |
| Composer messaggio | NON VERIFICATO | | serve una chat aperta |
| Pulsante invio | NON VERIFICATO | | idem |
| Interstitial "aggiorna WhatsApp Web" | non osservato | | Q43 |
| Popup/promo dismissibili | 2 banner visti (notifiche disattivate, promo broadcast), chiudibili con `[data-icon='x']` | | Q46: dismissibili, non bloccanti |

**`aria-label` reale del pannello: `"Lista delle chat"`.** Il candidato originale cercava
`"Elenco chat"` — vicino e inutile. È la ragione per cui questo catalogo esiste.

## Pannello conversazione — VERIFICATO (Step 1b, chat reale con 35 messaggi)

| Elemento | Selettore VERO | Copertura | Note |
|---|---|---|---|
| Contenitore conversazione | `#main` | presente | |
| Messaggio (nodo canonico) | `#main [data-id]` | 35/35 | porta anche `data-testid="conv-msg-<id>"`: stesso nodo, due modi |
| ~~Bolla inbound~~ | ~~`div.message-in`~~ | **0** | **classe rimossa da WhatsApp** |
| ~~Bolla outbound~~ | ~~`div.message-out`~~ | **0** | idem, su qualsiasi tag |
| Testo con metadati | `[data-pre-plain-text]` | 17/35 | contiene `[ora, data] Nome:` — solo messaggi di testo |
| Composer | `[contenteditable='true'][data-lexical-editor]` | 1 | `aria-label="Digita un messaggio per <nome>"` |
| Virtualizzazione conversazione | `data-virtualized` | 35/35 | **anche la conversazione è virtualizzata** |

**WhatsApp Web usa ora l'editor Lexical** (`data-lexical-editor`, `data-lexical-managed-linebreak`).
Rilevante per `human_type`: Lexical intercetta l'input, quindi il comportamento di
Shift+Enter per gli a-capo **va confermato su un invio vero**, non dato per scontato.

### Direzione inbound/outbound — il problema centrale, risolto con 3 segnali

`message-in`/`message-out` non esistono più e **le classi rimaste sono offuscate e non
discriminano**: `xa0aww2` compare sia su inbound sia su outbound. Una prima analisi
automatica l'aveva indicata come discriminante — era un artefatto di campione piccolo
(4 OUT contro 2 IN). Verificata e scartata.

| # | Segnale | Copertura | Robustezza |
|---|---|---|---|
| 1 | `span[aria-label='Tu:']` presente → nostro; altro `aria-label$=':'` → loro | ~80% | semantico ma **localizzato IT**; assente sui blocchi consecutivi |
| 2 | `[data-icon='tail-out']` / `[data-icon='tail-in']` | 15/35 | sicuro ma solo sul primo messaggio di ogni blocco |
| 3 | `data-id`: `3A…` (20 char) → IN · `A5…` (32 char) → OUT | 35/35 | **non documentato**; coerente 12/12 coi tail. Mai da solo |

**Regola di combinazione, volutamente asimmetrica**: un messaggio è "nostro" solo se
almeno un segnale dice OUT **e nessuno** dice IN. Discordanza o assenza totale di
segnali ⇒ vale **inbound**, quindi si legge.

Il motivo è che i due errori non costano uguale: classificare un nostro messaggio come
inbound fa leggere qualche messaggio in più (al peggio si trova uno STOP vecchio e non
si invia); classificare un inbound come nostro fa **fermare la lettura prima di uno STOP
appena arrivato**, e si scrive a chi ha chiesto di smettere. In dubbio si legge.

Misurato su chat reale: **0 messaggi con segnali discordanti**.

### Trappola: `msg-container` e `[data-id]` sono nodi ANNIDATI

Cercarli insieme (`"[data-testid='msg-container'], [data-id]"`) restituisce **due nodi
per messaggio**: la lista raddoppia e il duplicato senza `data-id` decide con un segnale
in meno. Il selettore corretto usa due alias dello **stesso** nodo:
`"#main [data-id], #main [data-testid^='conv-msg-']"`.

## Spunte di consegna (Q39) — RISOLTA, ma non come previsto

Nessuno dei `data-icon='status-check' / 'status-dblcheck' / 'status-time'` esiste più
(0 nodi su 35 messaggi). Lo stato è esposto come **ARIA testuale**:
`aria-label` ` Consegnato ` / ` Letto ` / ` In attesa `.

Verificato: `[aria-label*='Consegnato']` aggancia su un messaggio inviato davvero.
**È testo localizzato**: su interfaccia non italiana smette di funzionare. Accettabile
in MVP (Q6: solo italiano), da rivedere in M1 se il cliente cambia lingua.

Gli unici `data-icon` nel pannello sono `tail-in`, `tail-out`, `ptt-status` (stato dei
vocali), più icone di interfaccia.

## Ricerca e apertura chat — VERIFICATO, con una trappola grave

Casella di ricerca: `[role='textbox']` (`[data-testid='chat-list-search']` **non esiste**).

**Le intestazioni di sezione sono `[role='row']` esattamente come le chat.** Nei risultati
di ricerca la riga 0 è l'intestazione `"Chat"`, la riga 2 è `"Gruppi in comune"`. Cliccare
`righe[0]` non apre nulla — è successo davvero al primo tentativo.

Peggio: le righe sotto `"Gruppi in comune"` sono **gruppi**, fuori perimetro. Una selezione
per posizione poteva aprirne uno. La navigazione corretta è **per sezione**: trovare
l'intestazione `"Chat"` e prendere la riga successiva, verificando che non sia a sua volta
un'intestazione (`Chat`, `Gruppi in comune`, `Contatti`, `Messaggi`).

Vale anche per lo scan di PoC-3: iterare `[role='row']` conta le intestazioni come chat.

## Virtualizzazione della lista (Q40) — VERIFICATO, e più severa del previsto

**485 chat dichiarate** (`aria-rowcount="485"` sul `[role='grid']`) · **67-70 righe nel DOM**
(due misure indipendenti a 3 minuti di distanza) · **~14% renderizzato**.

Il piano stimava 30-100 chat totali: l'ordine di grandezza reale è **5× più grande**.
Conseguenze operative:
- lo scan di PoC-3 deve scrollare, e serve una strategia di terminazione (Task 6);
- il criterio "20 inbound raccolti" resta valido, ma il tempo di scan va misurato:
  se lo scan completo di 485 chat costa minuti, il watcher va ripensato per M1;
- una chat vecchia è raggiungibile **solo** via ricerca, non via scroll ragionevole →
  rafforza la strategia "apertura per numero" di PoC-2a rispetto allo scroll.

## Schermata QR — MAI OSSERVATA

Il 27/07 la sessione è stata stabilita **a mano** da Tommaso prima che lo script
guardasse, quindi nessuna schermata QR è stata catalogata. `canvas` non aggancia nulla
a sessione attiva (atteso). Se PoC-1 provoca un logout nei 14 giorni, il primo rilancio
di `poc1_login.py` è l'occasione per catalogarla: lo script fa comunque screenshot
quando non riconosce la schermata.

## Incidente 27/07 — falso "schermata ignota" (lezione, non aneddoto)

`poc1_login.py` è morto con *"Né lista chat né QR: schermata non prevista"* mentre lo
screenshot mostrava una sessione **perfettamente loggata e funzionante**. Causa: dava
**8 secondi** alla lista chat, e un profilo Chromium freddo al primo avvio impiega di
più (download + costruzione app + sync iniziale di 485 chat).

Il rischio non era perdere 30 secondi: era **la diagnosi sbagliata**. La conclusione
naturale davanti a quell'errore era "i selettori sono sbagliati, WhatsApp ha cambiato
il DOM" — falsa, e avrebbe portato a riscrivere selettori che funzionavano benissimo.
Timeout portati a 90s/30s, e il messaggio d'errore ora dice esplicitamente di
controllare lo screenshot prima di incolpare i selettori.

## Q42 — direzione dell'ultimo messaggio in sidebar: `<svg><title>`, non `data-icon`

Le doppie spunte si vedono a schermo ma **nessuna riga ha un `data-icon`**: sono
`<svg>` con un `<title>` interno. Ecco perché due probe di fila le hanno mancate —
filtravano i nodi per attributo, e quegli `<svg>` non ne hanno di utili.

Valori osservati: `wds-ic-read` (letto), `wds-ic-delivered` (consegnato). Attesi per
simmetria ma non ancora visti: `wds-ic-sent`, `wds-ic-pending`.

**Presenza di un `wds-ic-*` nella riga ⇒ l'ultimo messaggio è NOSTRO.** Misurato su
68 righe: 5 `read`, 1 `delivered`, 62 senza icona (ultimo messaggio dell'altro).

## Q19 — nome vs numero: RISOLTA

Su 68 chat, **8 hanno il titolo numerico** (contatto non in rubrica → WhatsApp mostra il
numero). Il resto mostra il nome della rubrica. `title_is_number` nello scan lo distingue,
ed è il segnale che in M1 servirà a decidere se un `chat_title` è PII da mascherare.

## Marcatori di direzione del testo nei `title` — sporcano i confronti

WhatsApp inserisce `U+202A`/`U+202C` (e simili) dentro gli attributi `title`. Invisibili
a schermo, ma rompono confronti, ricerche e — su console Windows cp1252 — **uccidono lo
script** con `UnicodeEncodeError`. Lo scanner li rimuove; `_common` riconfigura stdout con
`errors='replace'`. Non è teorico: è successo il 27/07, e sarebbe successo comunque in
mezzo al PoC, visto che i nomi delle chat sono pieni di emoji.

## Limite noto dello scan: 46 anteprime vuote su 68

Il 68% delle righe scansionate non espone l'anteprima. Non blocca (l'anteprima serve solo
a individuare candidati, e non è mai stata una garanzia di opt-out), ma **va nel report**
come limite misurato, non nascosto. Da capire in M1 se dipende dal tipo di ultimo messaggio
(media, vocali, eventi di sistema) o dalla virtualizzazione.

## Prossimo passo obbligato

**Step 1b: aprire UNA chat dell'allowlist** e rifare il dump sul pannello conversazione.
Senza, `JS_TAIL`, `TICK_SEL` e `HISTORY_SEL` restano non verificati e **nessun invio
può partire**: sarebbe un invio con la guardia opt-out non provata.
