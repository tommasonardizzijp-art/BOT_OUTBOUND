"""La sidebar di WhatsApp Web: leggerne le righe e scorrerla come una mano.

I NUMERI DI QUESTO MODULO SONO MISURATI, NON MODELLATI. Vengono da
scripts/registra_scroll_umano.py (1660 eventi wheel registrati sull'hardware di
Tommaso l'11/08): verdetto TRACKPAD, 16,7 ms mediani fra eventi, picchi 193-342
px, gesti da 5.700 a 24.000 px. Il motore inbox Instagram usa ancora un modello
scritto a mano (SCATTO_MAX_PX=60) che quelle misure hanno smentito: non
ereditarlo, ereditare le misure.

REGOLA DI DISEGNO ereditata dal motore inbox Instagram (inbox_browser/pagina.py):
il JS raccoglie, Python decide. Nessuna coordinata cablata: le righe grezze
portano titolo e posizione, e righe_dalla_sidebar decide quali sono chat vere.

Differenza rispetto a Instagram: il contenitore della lista qui e' `#pane-side`,
verificato e stabile (wa-dom-catalog.md, misurato PoC-5: left=65, right=474,
w=409; tre selettori indipendenti agganciano lo stesso nodo, che porta anche
role="grid" e aria-rowcount). Non serve la ricerca euristica del contenitore
(scegli_contenitore) che il motore Instagram usa per distinguere la lista dal
pannello della conversazione -- ma se #pane-side manca non si tira a indovinare
un altro elemento: nessuno scorrimento, e lo si dice nei log.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

from loguru import logger

from app.browser import whatsapp_selectors as sel
from app.services.wa_discover.classifica import titolo_e_numero

# Dalle misure vere: i picchi osservati stanno fra 193 e 342 px per evento.
PICCO_MIN_PX = 150
PICCO_MAX_PX = 342
# 16,7 ms e' la mediana misurata: e' il ritmo di frame del browser, non una
# scelta di design. Il caso qui non c'entra quasi niente.
PAUSA_MIN_S = 0.010
PAUSA_MAX_S = 0.030
# Sotto una schermata: sopra il buffer renderizzato si perdono righe in silenzio
# (lista virtualizzata, stessa trappola del motore Instagram).
FRAZIONE_PASSO_MIN = 0.6
FRAZIONE_PASSO_MAX = 0.8
PROB_RIMBALZO = 0.12


@dataclass
class StatoScorrimento:
    altezza: int | None
    al_fondo: bool
    # Il totale di chat dichiarato da WhatsApp (aria-rowcount su #pane-side).
    # Serve al chiamante per confrontarlo col raccolto: e' cosi' che si scopre
    # una raccolta parziale invece di festeggiarla come "completata".
    rowcount: int | None


def piano_scroll_misurato(px_totali: int) -> list[tuple[int, float]]:
    """Il gesto come sequenza di (pixel, pausa). Profilo a campana.

    La campana (piano -> veloce -> piano) e' cio' che i gesti registrati
    mostrano: una sequenza di scatti tutti uguali e' un motore, non una mano.
    Il picco della campana si estrae nel range MISURATO, invece di essere
    tagliato a un massimo inventato.
    """
    if px_totali <= 0:
        return []

    picco = random.uniform(PICCO_MIN_PX, PICCO_MAX_PX)
    # Quanti eventi servono: l'area sotto una campana di ampiezza `picco` con
    # `n` eventi vale circa picco * n * 2/pi.
    quanti = max(6, round(px_totali / (picco * 2 / math.pi)))

    pesi = [math.sin(math.pi * (i + 0.5) / quanti) for i in range(quanti)]
    totale = sum(pesi) or 1.0

    piano: list[tuple[int, float]] = []
    for peso in pesi:
        delta = px_totali * peso / totale * random.uniform(0.85, 1.15)
        piano.append((max(1, min(PICCO_MAX_PX, round(delta))),
                      random.uniform(PAUSA_MIN_S, PAUSA_MAX_S)))

    if random.random() < PROB_RIMBALZO and len(piano) > 3:
        dove = random.randint(len(piano) // 2, len(piano) - 1)
        piano.insert(dove, (-random.randint(8, 40),
                            random.uniform(PAUSA_MIN_S, PAUSA_MAX_S)))
    return piano


def righe_dalla_sidebar(grezze: list[dict], altezza_viewport: int) -> list[dict]:
    """Solo le righe che sono davvero chat visibili. Funzione pura."""
    fuori = []
    for r in grezze or []:
        if not (r.get("titolo") or "").strip():
            continue
        if float(r.get("top", 0)) >= altezza_viewport:
            continue
        fuori.append(r)
    return fuori


# Scan della sidebar: nessun click su una riga, e' sola lettura per costruzione
# (vincolo del piano, non solo una scelta locale). Riusa la stessa logica gia'
# scritta per scan_chat_list in whatsapp_page.py -- le INTESTAZIONI di sezione
# ('Chat', 'Gruppi in comune', ...) sono [role='row'] identiche alle righe vere:
# si filtrano richiedendo un cell-frame-title dentro la riga, o la riga speciale
# "chat con se stessi" che non ne ha uno (vedi _JS_SCAN_CHAT_LIST in
# whatsapp_page.py e il commento sopra ROW_MARKER in whatsapp_selectors.py).
# Stessi marcatori bidi di _BIDI_MARKERS/whatsapp_page.py: WhatsApp li infila
# nei titoli che contengono numeri, invisibili a schermo ma pericolosi nei
# confronti testuali.
_JS_SCAN_SIDEBAR = """(args) => {
    const pane = document.querySelector(args.pane);
    if (!pane) return null;

    const pulisci = (s) => (s || '').replace(/[\\u202a-\\u202e\\u2066-\\u2069]/g, '').trim();

    const rows = Array.from(pane.querySelectorAll(args.row))
      .filter(r => r.querySelector(args.rowMarker) || r.matches(args.yourself));

    return {
        viewport_h: window.innerHeight,
        righe: rows.map((r, i) => {
            const t = r.querySelector(args.title);
            const rect = r.getBoundingClientRect();
            return {
                position: i,
                titolo: pulisci(t ? (t.getAttribute('title') || t.innerText) : ''),
                top: Math.round(rect.top),
            };
        }),
    };
}"""

# Stato/geometria di #pane-side: usata sia PRIMA dello scroll (per posizionare
# il mouse e calcolare i pixel del passo) sia DOPO (per decidere se la lista e'
# cresciuta). aria-rowcount si legge dallo stesso nodo di #pane-side: i tre
# selettori del catalogo (CHATLIST) agganciano tutti lo stesso elemento, che
# porta gia' role="grid" -- ma si cerca comunque un [role='grid'] annidato come
# ripiego, per non dipendere da quel dettaglio se WhatsApp lo cambiasse.
_JS_STATO_PANE = """(selettore) => {
    const pane = document.querySelector(selettore);
    if (!pane) return null;
    const grid = pane.matches("[role='grid']") ? pane : pane.querySelector("[role='grid']");
    const rowcountRaw = grid ? grid.getAttribute('aria-rowcount') : null;
    const r = pane.getBoundingClientRect();
    return {
        altezza: pane.scrollHeight,
        alFondo: (pane.scrollHeight - pane.scrollTop - pane.clientHeight) < 50,
        rowcount: rowcountRaw ? parseInt(rowcountRaw, 10) : null,
        left: Math.round(r.left), top: Math.round(r.top),
        w: Math.round(r.width), h: Math.round(r.height),
        clientHeight: pane.clientHeight,
    };
}"""


async def scan_sidebar(page) -> list[dict]:
    """Le righe attualmente nel DOM della sidebar. Da richiamare a ogni passo
    di scorrimento (lista virtualizzata: le righe fuori dal buffer renderizzato
    spariscono dal DOM, stessa trappola gia' pagata dal motore Instagram).

    Ogni riga porta `titolo_e_numero`: dice al chiamante se il titolo E' gia'
    il numero (39% delle chat su Primero, PoC-5) -- sono le righe per cui NON
    serve aprire il pannello info (Task 5) e i 5,3s medi che costa.

    ⚠️ `posizione` E' UN DATO DIAGNOSTICO, NON UNA COORDINATA CLICCABILE. Aprire
    una chat la segna letta e WhatsApp riordina la lista per ultima attivita':
    l'indice preso a inizio scan smette di puntare alla chat attesa (PoC-4:
    mismatch in 4-5 aperture su 8). Chi apre una riga deve ri-risolverla per
    CONTENUTO immediatamente prima del click e verificare dopo, come fa
    inbox_browser.pagina.apri_riga. Serve solo per i log e per accorgersi dei
    riordini, mai per decidere dove cliccare.
    """
    args = {"pane": sel.CHATLIST[0], "row": sel.ROW, "rowMarker": sel.ROW_MARKER,
            "yourself": sel.YOURSELF_ROW, "title": sel.TITLE}
    dati = await page.evaluate(_JS_SCAN_SIDEBAR, args)
    if dati is None:
        logger.warning(
            f"[WaDiscover] {sel.CHATLIST[0]} non trovato — nessuna riga raccolta, "
            "nessun tentativo su un altro contenitore"
        )
        return []

    grezze = dati.get("righe") or []
    valide = righe_dalla_sidebar(grezze, altezza_viewport=dati.get("viewport_h") or 0)
    return [
        {"posizione": r["position"], "titolo": r["titolo"],
         "titolo_e_numero": titolo_e_numero(r["titolo"])}
        for r in valide
    ]


async def totale_dichiarato(page) -> int | None:
    """Quante chat dichiara WhatsApp (`aria-rowcount`), SENZA scorrere nulla.

    Esiste perche' l'orchestratore deve conoscere il totale *prima* di iniziare
    -- per dimensionare il giro, e soprattutto per confrontarlo col raccolto a
    fine scan: e' l'unico modo di accorgersi di una raccolta parziale invece di
    chiuderla come "completata". Senza questa funzione l'unico modo di leggerlo
    sarebbe chiamare `scorri_sidebar`, cioe' MODIFICARE lo stato che si voleva
    solo misurare.

    Misurato: 291 su Primero, 485 sul numero personale (PoC-4/PoC-5). None se il
    pannello non c'e' o non espone l'attributo -- e None non e' zero: chi legge
    deve trattarlo come "non lo so", non come "nessuna chat".
    """
    stato = await page.evaluate(_JS_STATO_PANE, sel.CHATLIST[0])
    if stato is None:
        logger.warning(
            f"[WaDiscover] {sel.CHATLIST[0]} non trovato — totale dichiarato ignoto"
        )
        return None
    return stato.get("rowcount")


async def scorri_sidebar(page) -> StatoScorrimento:
    """Un passo di scorrimento della sidebar, sempre inferiore a una schermata.

    Si scorre con la ROTELLA sul contenitore, non assegnando `scrollTop`: un
    salto istantaneo e' una firma che nessun dito produce (stesso motivo di
    inbox_browser.pagina.scorri). A differenza del motore Instagram non serve
    scegliere fra piu' contenitori candidati -- #pane-side e' l'UNICO
    contenitore della lista ed e' stabile -- ma se manca non si indovina un
    altro elemento: nessuno scorrimento, log esplicito.
    """
    prima = await page.evaluate(_JS_STATO_PANE, sel.CHATLIST[0])
    if prima is None:
        logger.warning(
            f"[WaDiscover] {sel.CHATLIST[0]} non trovato — nessuno scorrimento, "
            "nessun tentativo su un altro contenitore"
        )
        return StatoScorrimento(altezza=None, al_fondo=False, rowcount=None)

    frazione = random.uniform(FRAZIONE_PASSO_MIN, FRAZIONE_PASSO_MAX)
    px = int(prima["clientHeight"] * frazione)

    # Il puntatore deve stare SOPRA la lista: la rotella scorre l'elemento
    # sotto il mouse, non quello "selezionato".
    x = prima["left"] + prima["w"] * random.uniform(0.3, 0.7)
    y = prima["top"] + prima["h"] * random.uniform(0.3, 0.7)
    await page.mouse.move(x, y, steps=random.randint(5, 15))

    for delta, pausa in piano_scroll_misurato(px):
        await page.mouse.wheel(0, delta)
        await page.wait_for_timeout(int(pausa * 1000))

    dopo = await page.evaluate(_JS_STATO_PANE, sel.CHATLIST[0])
    if dopo is None:
        return StatoScorrimento(altezza=None, al_fondo=False, rowcount=None)
    return StatoScorrimento(altezza=dopo["altezza"], al_fondo=dopo["alFondo"],
                            rowcount=dopo["rowcount"])
