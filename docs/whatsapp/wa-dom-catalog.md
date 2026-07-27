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
| Titolo chat (nome o numero) | NON VERIFICATO | | Q19 aperta: serve leggere una riga |
| Badge non letti | NON VERIFICATO | | Q41 aperta |
| Preview ultimo messaggio | NON VERIFICATO | | |
| Direzione ultimo messaggio (in/out) | NON VERIFICATO | | Q42: **le spunte in sidebar NON usano `data-icon`** — vedi sotto |
| Casella di ricerca | `[role='textbox']` (1) | | `[data-testid='chat-list-search']` **non esiste**; `div[contenteditable='true']` **non aggancia** (0) |
| Composer messaggio | NON VERIFICATO | | serve una chat aperta |
| Pulsante invio | NON VERIFICATO | | idem |
| Interstitial "aggiorna WhatsApp Web" | non osservato | | Q43 |
| Popup/promo dismissibili | 2 banner visti (notifiche disattivate, promo broadcast), chiudibili con `[data-icon='x']` | | Q46: dismissibili, non bloccanti |

**`aria-label` reale del pannello: `"Lista delle chat"`.** Il candidato originale cercava
`"Elenco chat"` — vicino e inutile. È la ragione per cui questo catalogo esiste.

## Pannello conversazione — NON VERIFICATO (base della guardia pre-invio, Task 8)

Il probe ha misurato **0** per tutti questi selettori, ma **senza alcuna chat aperta**:
il risultato è privo di significato in entrambe le direzioni. Va rifatto con lo Step 1b.

| Elemento | Ipotesi corrente | Conteggio a chat CHIUSA | Stato |
|---|---|---|---|
| Contenitore conversazione | `#main` | 0 | NON VERIFICATO |
| Bolla inbound | `div.message-in` | 0 | NON VERIFICATO |
| Bolla outbound | `div.message-out` | 0 | NON VERIFICATO |
| Identificativo messaggio | `[data-id]` | 0 | NON VERIFICATO |
| Icone di stato | `[data-icon='status-*']` | 0 | **contraddetto**, vedi sotto |

**Verdetto su `JS_TAIL` del Task 8: ancora ignoto.** È il rischio principale aperto:
se non aggancia, ritorna lista vuota, la guardia non trova mai uno STOP e **lascia
passare tutto sembrando funzionare**. La sentinella `null` in `poc2_send.py` protegge
da questo, ma va provata sul campo prima di fidarsene.

## `data-icon`: 7 valori, nessuna spunta — SEGNALE DA APPROFONDIRE

Valori presenti a chat chiusa: `storefront`, `settings-refreshed`, `wa-wordmark`,
`new-chat-outline`, `x`, `wds-smb-ill-start-a-chat`, `lock-outline`.

Nessuno `status-check` / `status-dblcheck` / `status-time`, **eppure lo screenshot
mostra doppie spunte blu su più righe della sidebar**. Conclusione: in questa versione
le spunte della sidebar sono rese in altro modo (SVG inline senza `data-icon`, o
pseudo-elemento CSS). `TICK_SEL` del Task 8 va ricavato dal pannello conversazione
nello Step 1b — e se anche lì manca, Q39 (lettura della spunta di consegna) **non è
risolvibile via DOM** e va dichiarata NO nel report invece che aggirata.

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

## Prossimo passo obbligato

**Step 1b: aprire UNA chat dell'allowlist** e rifare il dump sul pannello conversazione.
Senza, `JS_TAIL`, `TICK_SEL` e `HISTORY_SEL` restano non verificati e **nessun invio
può partire**: sarebbe un invio con la guardia opt-out non provata.
