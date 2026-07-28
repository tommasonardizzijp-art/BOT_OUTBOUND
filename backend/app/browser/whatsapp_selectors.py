"""Selettori DOM di WhatsApp Web. SOLO costanti, nessuna logica.

Separato da whatsapp_page.py di proposito: e' la parte che WhatsApp rompera'
per prima, e deve poter essere aggiornata senza rileggere la logica.

Ogni costante porta la misura fatta in M0 (27/07). Chi la aggiorna deve poter
capire se e' ancora vera. Fonte: docs/whatsapp/wa-dom-catalog.md e gli script
GIA' VERIFICATI sul DOM reale in scripts/poc_wa/ (poc2_open.py, poc2_send.py,
poc3_scan.py, _common.py) -- non le ipotesi della loro prima stesura.

TRE COSE CHE NON ESISTONO, e che sono state cercate a lungo:
  div.message-in / div.message-out  -> 0 nodi su 35 messaggi
  data-icon='status-*'              -> non esiste nella versione attuale, le
                                        spunte sono aria-label testuali
  [role='listitem']                 -> le righe della lista sono [role='row']
"""

# Pannello lista chat. "#pane-side" e' il selettore VERIFICATO il 27/07 (tre
# selettori indipendenti agganciavano lo stesso nodo: robusto). I due
# fallback non sono mai stati necessari ma restano a costo zero.
CHATLIST = ["#pane-side", "[data-testid='chat-list']", "[role='grid']"]

# Schermata QR: MAI OSSERVATA in M0 (il 27/07 la sessione era gia' loggata a
# mano da Tommaso prima che lo script guardasse). Questi due selettori sono
# IPOTESI non misurate, non un catalogo: da verificare al primo re-login
# vero, catturando anche uno screenshot (come gia' fa poc1_login.py negli
# script PoC quando non riconosce la schermata).
QR = ["canvas[aria-label*='scan']", "div[data-ref]"]

# Casella di ricerca. VERIFICATO 27/07: [role='textbox'] aggancia (1 nodo).
# [data-testid='chat-list-search'] NON ESISTE -- era l'ipotesi della prima
# stesura di poc2_open.py, corretta lo stesso giorno contro il DOM reale. I
# due selettori aria-label restano come fallback multilingua, mai esercitati.
SEARCH = ["[role='textbox']", "[data-testid='chat-list-search']",
          "div[aria-label*='Cerca']", "div[aria-label*='Search']"]

# Composer del messaggio. VERIFICATO 27/07 (Step 1b, chat reale con 35
# messaggi): WhatsApp Web usa ora l'editor Lexical (data-lexical-editor).
# "div[contenteditable='true'][data-tab='10']" -- l'ipotesi della prima
# stesura -- non e' mai stato verificato e non compare piu' negli script.
COMPOSER = ["[contenteditable='true'][data-lexical-editor='true']",
            "div[aria-label^='Digita un messaggio']",
            "div[aria-label*='Type a message']",
            "footer [contenteditable='true']"]

# Contenitore della conversazione aperta. VERIFICATO: presente su ogni chat
# con una conversazione caricata.
MAIN = "#main"

# Nodo canonico di un messaggio nel pannello conversazione. VERIFICATO 35/35
# su una chat reale (27/07).
# ATTENZIONE -- trappola gia' presa (wa-dom-catalog.md): "#main
# [data-testid='msg-container']" e "#main [data-id]" sono nodi ANNIDATI, non
# lo stesso nodo. Cercarli insieme in un solo selettore raddoppia la lista
# (il duplicato senza data-id perde un segnale di direzione). Il nodo che
# porta data-id porta ANCHE data-testid="conv-msg-<id>": i due alias qui
# sotto individuano lo STESSO elemento, quindi l'OR CSS non duplica nulla.
MSG_ROW = "#main [data-id], #main [data-testid^='conv-msg-']"

# Righe della lista chat. [role='listitem'] NON ESISTE: le righe sono
# [role='row'] (67-70 nel DOM su 485 chat dichiarate -- ~14% renderizzato,
# la lista e' virtualizzata).
ROW = "[role='row']"
# Le INTESTAZIONI di sezione ('Chat', 'Gruppi in comune', ...) sono
# [role='row'] identiche alle chat: si filtrano richiedendo un cell-frame
# dentro la riga (o la riga speciale "chat con se stessi", che non ne ha uno).
ROW_MARKER = "[data-testid='cell-frame-title']"
YOURSELF_ROW = "[data-testid='message-yourself-row']"

# Titolo (nome o numero). L'attributo `title` porta il nome INTERO; l'inner
# text e' troncato dal CSS con i puntini.
TITLE = "[data-testid='cell-frame-title'] span[title], [data-testid='cell-frame-title'] span[dir='auto']"
UNREAD_BADGE = "[data-testid='icon-unread-count']"
# L'attributo `title` porta l'anteprima INTERA, non troncata. VERIFICATO:
# 46 righe su 68 (68%) non la espongono affatto -- limite noto e riportato,
# non un bug di questo selettore.
PREVIEW = "[data-testid='last-msg-status']"
MUTED = "[data-testid='mute-notifications-refreshed']"

# Etichette delle intestazioni di sezione nei risultati di ricerca (IT/EN).
# Senza questo filtro "la prima riga" e' l'intestazione "Chat" e cliccarla
# non apre nulla -- successo davvero il 27/07.
SEARCH_RESULT_HEADERS = frozenset({
    "chat", "chats", "gruppi in comune", "groups in common",
    "contatti", "contacts", "messaggi", "messages", "altri contatti",
})

# Spunte di consegna. Q39 RISOLTA il 27/07: nessuno dei data-icon='status-*'
# esiste piu' (0 nodi su 35 messaggi) -- lo stato e' esposto come ARIA
# testuale. ATTENZIONE: testo LOCALIZZATO IN ITALIANO. Rompere su un
# cliente non italiano e' un quando, non un se (SDD A4).
TICKS = ["[aria-label*='Consegnato']", "[aria-label*='Letto']", "[aria-label*='In attesa']"]

# data_id: 20 caratteri con prefisso '3A' -> inbound; 32 con 'A5' -> outbound.
# Copertura 100% su 35 messaggi, coerente 12/12 con i tail. NON documentato da
# WhatsApp: si usa come segnale, mai da solo (vedi classify_direction).
DATA_ID_IN_PREFIX, DATA_ID_IN_LEN = "3A", 20
DATA_ID_OUT_PREFIX, DATA_ID_OUT_LEN = "A5", 32

# Indicatore di sincronizzazione (A9/FM16): NON CATALOGATO.
# Si cattura alla prima riconnessione: farlo apposta richiede un re-scan del
# QR, che azzera PoC-1. Finche' e' vuoto, sync_state() torna 'unknown'.
SYNC_INDICATOR: list[str] = []
