"""Aprire una chat e leggere il pannello info, senza sbagliare persona.

LA TRAPPOLA CHE QUESTO MODULO ESISTE PER RISOLVERE (misurata, PoC-4): aprire
una chat la segna letta, e WhatsApp riordina la sidebar per ultima attivita'.
Un indice di riga preso a inizio scan smette di puntare alla chat attesa --
mismatch in 4-5 aperture su 8. La soluzione NON e' ricalcolare meglio gli
indici, e' quella gia' adottata da `inbox_browser.pagina.apri_riga` su
Instagram: la riga si ri-risolve per CONTENUTO (titolo) immediatamente prima
del click, poi si VERIFICA dopo il click che cio' che si e' aperto sia cio'
che si voleva. Se la verifica fallisce non si scrive niente: la riga si
ritenta al giro dopo, mai un dato attribuito alla persona sbagliata.

Sequenza e selettori dal PoC-4 (scripts/poc_wa/poc4_info_panel.py, gia'
eseguito contro il DOM reale di Primero): click sulla riga -> leggi l'header
-> click sull'header (apre il pannello info) -> leggi il pannello -> Escape
x2 per richiuderlo. Pannello apribile nel 95% dei casi (19/20); numero
leggibile nel 100% delle chat 1:1 e nello 0% dei gruppi (corretto: un gruppo
non ha un numero singolo).

Selettori locali e non in app/browser/whatsapp_selectors.py: questo task ha
per vincolo di toccare solo questo file e il suo test. Se un altro consumatore
dovesse nascere, spostarli nel catalogo comune.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from loguru import logger

from app.browser import human_input
from app.services.inbox_browser.testo import normalizza_nome
from app.utils.phone_pseudonym import PhoneNormalizationError, normalize_e164

# Header della conversazione. Le due forme del PoC-4 agganciano lo stesso
# nodo (header[data-testid='conversation-header'] E' l'unico <header> dentro
# #main): una query sola con l'OR CSS basta, non serve provarle in sequenza.
SEL_HEADER = "#main header, header[data-testid='conversation-header']"
# Pannello info del contatto/gruppo. 95% di successo nell'apertura (19/20,
# PoC-4): il 5% restante non e' un buco da tappare qui -- si ritorna comunque
# il titolo gia' verificato, solo senza numero (vedi apri_e_leggi).
SEL_DRAWER_INFO = "[data-testid='drawer-right']"

# Pazienza crescente dopo il click sulla riga, stesso principio di
# _ATTESE_HEADER_S in inbox_browser.pagina: un'attesa fissa scartava come
# "lista riordinata" righe in realta' corrette perche' il pannello arrivava
# dopo 1-2s. Valori identici: stesso fenomeno, stessa soluzione gia' pagata.
_ATTESE_HEADER_S = (0.8, 1.5, 2.5, 3.0)
# Il pannello info costa di piu' ad aprirsi dell'header (misurato PoC-4: 5,3s
# medi, coda 3,5-8,7s): la progressione copre quella coda senza bloccare il
# caso rapido, che esce alla prima lettura non vuota.
_ATTESE_PANNELLO_S = (1.0, 1.5, 2.0, 2.5)

# Un numero nel testo del pannello si riconosce dal prefisso '+': a differenza
# dei titoli (dove titolo_e_numero legge l'INTERA stringa), qui il numero
# convive in un blob di prosa con date, orari e conteggi partecipanti --
# "Ultimo accesso 11/08/2026 alle 14:35" contiene 8 cifre ma non e' un numero.
# Richiedere il '+' e' il modo semplice e sicuro di distinguerli: e' la forma
# ESATTA in cui WhatsApp mostra un numero salvato in E.164 nel pannello
# (misurato PoC-4), niente che una data o un conteggio possano produrre.
_CANDIDATO_NUMERO = re.compile(r"\+\d[\d\s\-./()]{4,}\d")


# Esiti possibili di un'apertura. Il tri-stato NON e' pedanteria: senza,
# "verifica post-click fallita" e "titolo verificato ma pannello non arrivato"
# sarebbero lo stesso valore di ritorno, e sono due casi OPPOSTI per chi
# orchestra il giro --
#   verifica fallita  -> NON salvare, la chat aperta e' un'altra persona;
#   verificata senza numero -> SALVARE, marcata "numero non letto": e' il caso
#     del 100% dei gruppi e la spec lo richiede esplicitamente (5.3/5.4).
# Appiattirli su None costringerebbe l'orchestratore a scegliere fra perdere
# tutti i gruppi e salvare righe attribuite alla persona sbagliata.
ESITO_VERIFICATA = "verificata"          # la chat aperta e' quella attesa
ESITO_NON_VERIFICATA = "non_verificata"  # se ne e' aperta un'altra: si rinuncia
ESITO_RIGA_ASSENTE = "riga_assente"      # non era piu' nel DOM: nessun click


@dataclass
class EsitoApertura:
    esito: str                    # ESITO_*
    numero: str | None            # E.164, None se non leggibile
    testo_pannello: str           # sempre una stringa, "" se non letto

    @property
    def salvabile(self) -> bool:
        """Si puo' scrivere a DB solo se il titolo e' stato verificato.

        Il numero puo' mancare -- quella riga si salva marcata, non si butta.
        """
        return self.esito == ESITO_VERIFICATA


def titolo_combacia(atteso: str | None, trovato: str | None) -> bool:
    """Verifica post-click. Se uno dei due manca, NON combacia: meglio
    rinunciare a una riga che salvare un numero attribuito alla persona
    sbagliata. Stessa regola e stessa funzione di normalizzazione di
    inbox_browser.pagina.nome_combacia (normalizza_nome toglie emoji, spazi
    doppi e marcatori invisibili -- semantica verificata, non assunta)."""
    a, b = normalizza_nome(atteso), normalizza_nome(trovato)
    return bool(a) and bool(b) and a == b


def numero_dal_pannello(testo: str | None) -> str | None:
    """Il numero E.164 se il testo del pannello ne contiene uno leggibile,
    altrimenti None. Riga per riga (non sull'intero blob): '+' e cifre non
    devono mai saldarsi a caso attraverso un a-capo.

    None e' la risposta corretta per un gruppo (0% di numeri leggibili,
    PoC-4) tanto quanto per un pannello che non si e' aperto: il chiamante
    non deve distinguere i due casi da qui, li distingue gia' a monte
    (apri_e_leggi ritorna testo_pannello vuoto quando il pannello manca).
    """
    for riga in (testo or "").split("\n"):
        for grezzo in _CANDIDATO_NUMERO.findall(riga):
            try:
                return normalize_e164(grezzo)
            except PhoneNormalizationError:
                continue
    return None


def titolo_da_header(testo: str | None, atteso: str | None) -> str | None:
    """Il nome della chat aperta, cercato fra TUTTE le righe dell'header.

    Non "la prima riga", ed e' una correzione pagata sul campo (collaudo 11/08,
    due difetti distinti che producevano lo stesso sintomo):

    1. Per un contatto senza foto profilo WhatsApp mette le INIZIALI come primo
       nodo di testo. Leggendo solo la prima riga si confrontava
       'Jack Santini - Edicola Tiburtina' con 'JS', e la riga veniva scartata
       come "si e' aperta un'altra persona".
    2. L'header resta quello della chat PRECEDENTE per qualche istante dopo il
       click. Su Instagram il motore aspetta il cambio di URL, ma su WhatsApp
       l'URL non cambia mai (resta web.whatsapp.com, verificato): quel trucco
       qui non e' disponibile.

    Cercare l'atteso fra tutte le righe risolve il primo caso, e per il secondo
    fa la cosa giusta: finche' l'header e' stale nessuna riga combacia, quindi
    la verifica fallisce e si continua ad aspettare invece di accettare la
    persona sbagliata.

    Quando nulla combacia ritorna comunque la riga piu' lunga, che serve al log:
    "aperto 'JS'" non dice niente a chi legge, "aperto 'Jack Santini'" si'.
    """
    righe = [r.strip() for r in (testo or "").split("\n") if r.strip()]
    if not righe:
        return None
    for riga in righe:
        if titolo_combacia(atteso, riga):
            return riga
    return max(righe, key=len)


# Titoli delle righe attualmente nel DOM, per la risoluzione per CONTENUTO
# immediatamente prima del click. Stessi marcatori di riga di sidebar.py
# (ROW_MARKER/YOURSELF_ROW filtrano le intestazioni di sezione), ma qui non
# si applica il filtro di viewport di righe_dalla_sidebar: la riga puo' stare
# appena sotto la piega, e human_click la scorre in vista da solo
# (scroll_into_view_if_needed) -- scartarla qui la renderebbe irraggiungibile
# anche quando cliccabile.
_JS_TITOLI_RIGHE = """(args) => {
    const pane = document.querySelector(args.pane);
    if (!pane) return null;
    const pulisci = (s) => (s || '').replace(/[\\u202a-\\u202e\\u2066-\\u2069]/g, '').trim();
    const rows = Array.from(pane.querySelectorAll(args.row))
      .filter(r => r.querySelector(args.rowMarker) || r.matches(args.yourself));
    return rows.map(r => {
        const t = r.querySelector(args.title);
        return pulisci(t ? (t.getAttribute('title') || t.innerText) : '');
    });
}"""

# Elemento cliccabile alla posizione risolta sopra. Due chiamate separate
# (prima i titoli, poi l'elemento) invece di una sola che ritorni entrambi:
# stesso schema a due passi di apri_riga, cosi' fra la lettura dei titoli e
# il click non si porta dietro nessuno stato che possa essere gia' stale.
_JS_RIGA_ALL_INDICE = """(args) => {
    const pane = document.querySelector(args.pane);
    if (!pane) return null;
    const rows = Array.from(pane.querySelectorAll(args.row))
      .filter(r => r.querySelector(args.rowMarker) || r.matches(args.yourself));
    return rows[args.idx] || null;
}"""

_JS_TESTO_ELEMENTO = """(selettore) => {
    const nodo = document.querySelector(selettore);
    return nodo ? nodo.innerText : null;
}"""

_JS_ELEMENTO_DA_SELETTORE = "(selettore) => document.querySelector(selettore)"


async def apri_e_leggi(page, titolo_atteso: str | None) -> EsitoApertura:
    """Apre la chat con questo titolo e legge il pannello info.

    Ritorna un EsitoApertura, non una coppia: chi orchestra il giro deve
    poter distinguere "si e' aperta la persona sbagliata" (non salvare, la
    lista si e' riordinata) da "e' la persona giusta ma il numero non si legge"
    (salvare comunque, marcata -- e' il caso del 100% dei gruppi e del 5% di
    pannelli che non si aprono). Con un solo None quei due casi collassano, e
    l'orchestratore e' costretto a scegliere fra perdere tutti i gruppi e
    salvare dati attribuiti alla persona sbagliata.

    `numero` e' None quando il numero non e' leggibile -- MAI un tentativo
    indovinato. `testo_pannello` e' sempre una stringa, "" se non si e'
    arrivati a leggere niente.
    """
    from app.browser import whatsapp_selectors as sel

    titolo_target = normalizza_nome(titolo_atteso)
    if not titolo_target:
        logger.warning("[WaDiscover] apri_e_leggi: titolo_atteso mancante — nessun click")
        return EsitoApertura(ESITO_RIGA_ASSENTE, None, "")

    args_righe = {"pane": sel.CHATLIST[0], "row": sel.ROW, "rowMarker": sel.ROW_MARKER,
                  "yourself": sel.YOURSELF_ROW, "title": sel.TITLE}
    titoli = await page.evaluate(_JS_TITOLI_RIGHE, args_righe)
    if not titoli:
        logger.warning(
            f"[WaDiscover] {sel.CHATLIST[0]} non trovato o senza righe — nessun click "
            f"per {titolo_atteso!r}"
        )
        return EsitoApertura(ESITO_RIGA_ASSENTE, None, "")

    indice_effettivo = next(
        (i for i, t in enumerate(titoli) if titolo_combacia(titolo_atteso, t)), None
    )
    if indice_effettivo is None:
        logger.warning(
            f"[WaDiscover] riga non trovata al momento del click per {titolo_atteso!r} — "
            "probabilmente scorsa fuori dal DOM virtualizzato o riordinata"
        )
        return EsitoApertura(ESITO_RIGA_ASSENTE, None, "")

    handle_riga = await page.evaluate_handle(
        _JS_RIGA_ALL_INDICE, {**args_righe, "idx": indice_effettivo}
    )
    elemento_riga = handle_riga.as_element()
    if elemento_riga is None:
        return EsitoApertura(ESITO_RIGA_ASSENTE, None, "")

    await human_input.human_click(page, elemento_riga)

    # Verifica post-click, pazienza crescente: si esce appena l'header
    # combacia, quindi il caso rapido resta rapido.
    titolo_trovato = None
    for attesa_s in _ATTESE_HEADER_S:
        await page.wait_for_timeout(int(attesa_s * 1000))
        candidato = titolo_da_header(await page.evaluate(_JS_TESTO_ELEMENTO, SEL_HEADER), titolo_atteso)
        if candidato:
            titolo_trovato = candidato
            if titolo_combacia(titolo_atteso, candidato):
                break

    if not titolo_combacia(titolo_atteso, titolo_trovato):
        logger.warning(
            f"[WaDiscover] verifica post-click fallita: atteso {titolo_atteso!r}, "
            f"aperto {titolo_trovato!r} — riga non salvata (si ritenta al giro dopo)"
        )
        return EsitoApertura(ESITO_NON_VERIFICATA, None, "")

    # Il titolo e' verificato: da qui in poi un pannello che non arriva (5%,
    # PoC-4) costa solo il numero, non l'intera riga gia' accertata.
    handle_header = await page.evaluate_handle(_JS_ELEMENTO_DA_SELETTORE, SEL_HEADER)
    elemento_header = handle_header.as_element()
    if elemento_header is None:
        logger.warning(
            f"[WaDiscover] header non piu' raggiungibile per aprire il pannello di "
            f"{titolo_atteso!r} — titolo verificato, numero non letto"
        )
        return EsitoApertura(ESITO_VERIFICATA, None, "")

    await human_input.human_click(page, elemento_header)

    testo_pannello = ""
    for attesa_s in _ATTESE_PANNELLO_S:
        await page.wait_for_timeout(int(attesa_s * 1000))
        testo = await page.evaluate(_JS_TESTO_ELEMENTO, SEL_DRAWER_INFO)
        if testo:
            testo_pannello = testo
            break

    # Richiude il pannello: sola lettura, non deve lasciare la UI alterata
    # per la chat successiva del giro (stesso gesto di poc4_info_panel.py).
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(200)
        await page.keyboard.press("Escape")
    except Exception:
        pass

    if not testo_pannello:
        logger.info(
            f"[WaDiscover] pannello info non arrivato per {titolo_atteso!r} — "
            "titolo verificato, numero non letto"
        )
        return EsitoApertura(ESITO_VERIFICATA, None, "")

    return EsitoApertura(ESITO_VERIFICATA, numero_dal_pannello(testo_pannello),
                         testo_pannello)
