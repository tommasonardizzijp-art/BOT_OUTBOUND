# Catalogo DOM WhatsApp Web — rilevato in M0

> **TEMPLATE NON COMPILATO.** Questo file e' lo scheletro del catalogo, creato durante
> il Task 5 senza una sessione WhatsApp collegata. Le celle sono vuote di proposito:
> nessun selettore qui dentro e' stato verificato contro il DOM reale. Vanno riempiti
> **eseguendo** `poc3_dump_dom.py` (Step 2) e poi `poc3_dump_dom.py --chat "+39..."`
> (Step 1b) su una sessione autenticata, leggendo i dump JSON prodotti in
> `artifacts/dom_dump_*.json` e `artifacts/conv_dump_*.json`, e trascrivendo qui i
> selettori che hanno effettivamente agganciato qualcosa. Non indovinare: una riga
> vuota e' un dato onesto ("non ancora verificato"), un selettore plausibile ma mai
> visto sul DOM e' un rischio nascosto per tutto M1 (che si baserà su questo file).

> Data rilevazione: <data> · Versione WhatsApp Web: <se visibile> · Lingua interfaccia: it
> Ogni voce riporta il selettore scelto E l'alternativa di fallback. Se una voce non
> e' stata trovata, si scrive "NON TROVATO": e' un dato per il gate, non una lacuna da nascondere.

| Elemento | Selettore primario | Fallback | Note/robustezza |
|---|---|---|---|
| Pannello lista chat | | | |
| Riga chat | | | |
| Titolo chat (nome o numero) | | | Q19: stabile nel tempo? |
| Badge non letti | | | Q41: testo/aria-label o solo CSS? |
| Preview ultimo messaggio | | | troncata a quanti caratteri? |
| Direzione ultimo messaggio (in/out) | | | Q42: icona spunta presente sugli outbound? |
| Timestamp riga | | | formato |
| Casella di ricerca | | | |
| Composer messaggio | | | |
| Pulsante invio | | | |
| Spunte messaggio inviato (orologio/1/2) | | | Q39 |
| Interstitial "aggiorna WhatsApp Web" | | | Q43 |
| Popup/promo dismissibili | | | Q46 |

## Pannello conversazione (dallo Step 1b — base della guardia pre-invio del Task 8)

| Elemento | Selettore rilevato | Conteggio nel dump | Note |
|---|---|---|---|
| Contenitore conversazione | | | `#main`? |
| Bolla inbound | | | `div.message-in` regge o va sostituito? |
| Bolla outbound | | | come si distingue da inbound |
| Icone di stato viste (`data-icon`) | | | → `TICK_SEL`, Q39 |
| Composer | | | |
| Segnale "chat con cronologia" | | | → `HISTORY_SEL`, Q37/Q38 |

**Verdetto su `JS_TAIL` del Task 8:** <non ancora verificato — eseguire lo Step 1b>

## Virtualizzazione della lista (Q40)
Righe nel DOM a riposo: <n> · chat totali sul numero: <n> · scroll necessario per vederle tutte: <sì/no, quanto>

## Titolo chat: nome vs numero (Q19)
Contatto in rubrica: <cosa mostra> · Contatto non in rubrica: <cosa mostra>
