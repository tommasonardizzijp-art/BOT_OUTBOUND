"""Interazione col DOM dell'inbox web.

Quattro vincoli misurati sul campo, non ipotizzati:

1. LISTA VIRTUALIZZATA. Instagram tiene nel DOM solo le righe vicine al viewport
   e rimuove le altre (misurato: il conteggio righe oscilla fra 72 e 96 mentre
   l'altezza cresce in modo monotono). Scorrere a salti piu' grandi del buffer fa
   perdere righe IN SILENZIO: nessun errore, solo contatti mancanti.

2. NESSUN INDICATORE DI CARICAMENTO. Misurato: 0 spinner su 10 giri di scroll. Il
   segnale utile e' l'ALTEZZA del contenitore, che cresce a ogni caricamento
   riuscito (1152 -> 1872 -> ... -> 5112). Il numero di righe NON e' utilizzabile.

3. IL CLICK E' PER COORDINATE. human_click calcola il riquadro, muove il mouse in
   5-15 passi, attende 50-150 ms, poi preme (human_input.py:99-107). Se in quella
   finestra arriva un DM, la lista scorre di una posizione e si apre la chat
   accanto: mouse.click riesce sempre, nessun errore. Da qui la verifica
   post-click obbligatoria.

4. CHAT NON LETTA = FONT-WEIGHT 600. Misurato nel Task 0 su 4 righe reali con 3
   riscontri indipendenti (probe DOM, screenshot, <title>): le chat non lette
   hanno il nome in font-weight 600, quelle lette sempre 400, mai un valore
   intermedio. Nessuna aria-label disponibile. Il pallino blu pieno e' conferma
   ridondante, non un segnale a se' stante. La spec del modulo apre SOLO le
   chat gia' lette, per non bruciare il badge dei non letti di Tommaso.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from loguru import logger

from app.browser import human_input
from app.services.inbox_browser.testo import (
    analizza_riga_lista, estrai_username_thread, normalizza_nome,
)

# Sotto una schermata: sopra il buffer renderizzato si perdono righe in silenzio.
PASSO_SCROLL_MIN = 0.6
PASSO_SCROLL_MAX = 0.8

# Attese a pazienza crescente prima di dichiarare qualcosa sulla fine lista.
ATTESE_S = (1, 2, 4, 8, 16)

_JS_RIGHE = """(nRighe) => {
    const righe = [...document.querySelectorAll('div[role="button"], div[tabindex="0"], a')]
      .filter(e => { const r = e.getBoundingClientRect();
        return r.left < 660 && r.top > 150 && r.height > 50 && r.height < 130 && r.width > 250; });

    // Segnale primario: un nodo di testo foglia con font-weight >= 600.
    const fontWeightAlto = (riga) => [...riga.querySelectorAll('span, div')]
      .filter(n => n.children.length === 0 && n.textContent.trim().length > 0)
      .some(n => parseInt(getComputedStyle(n).fontWeight, 10) >= 600);

    // Conferma ridondante: pallino pieno, non usata da sola per decidere.
    const pallinoPresente = (riga) => [...riga.querySelectorAll('*')].some(n => {
        const r = n.getBoundingClientRect();
        if (r.width < 4 || r.width > 16 || Math.abs(r.width - r.height) >= 3) return false;
        const stile = getComputedStyle(n);
        const raggio = parseFloat(stile.borderRadius);
        if (!(raggio > 0)) return false;
        const sfondo = stile.backgroundColor;
        return !!sfondo && sfondo !== 'transparent' && sfondo !== 'rgba(0, 0, 0, 0)';
    });

    return righe.slice(0, nRighe).map((e, i) => ({
        indice: i, testo: e.innerText,
        nonLetta: fontWeightAlto(e),
        pallinoConferma: pallinoPresente(e),
    }));
}"""

_JS_CONTENITORE = """() => {
    let box = null, best = 0;
    for (const e of document.querySelectorAll('div')) {
        const r = e.getBoundingClientRect();
        if (r.left > 700 || r.width < 200 || r.height < 300) continue;
        if (!/auto|scroll/.test(getComputedStyle(e).overflowY)) continue;
        if (e.scrollHeight > best) { best = e.scrollHeight; box = e; }
    }
    if (!box) return null;
    return {altezza: box.scrollHeight, top: box.scrollTop, visibile: box.clientHeight,
            alFondo: (box.scrollHeight - box.scrollTop - box.clientHeight) < 50};
}"""

_JS_HREF_THREAD = """() => [...document.querySelectorAll('a[href^="/"]')]
    .map(e => e.getAttribute('href'))"""

_JS_HEADER_THREAD = """() => {
    const t = [...document.querySelectorAll('span, div')]
      .filter(e => { const r = e.getBoundingClientRect();
        return r.left > 660 && r.top < 130 && e.children.length === 0
               && e.textContent.trim().length > 1; })
      .map(e => e.textContent.trim());
    return [...new Set(t)];
}"""


@dataclass
class RigaVisibile:
    indice: int
    nome: str | None
    ultimo_nostro: bool | None
    non_letta: bool
    testo_grezzo: str


@dataclass
class StatoScorrimento:
    altezza: int | None
    al_fondo: bool


def nome_combacia(atteso: str | None, trovato: str | None) -> bool:
    """Verifica post-click. Se uno dei due manca, NON combacia: meglio rinunciare
    a una riga che salvare dati attribuiti alla persona sbagliata."""
    a, b = normalizza_nome(atteso), normalizza_nome(trovato)
    return bool(a) and bool(b) and a == b


async def leggi_righe_visibili(page, lingua: str, quante: int = 30) -> list[RigaVisibile]:
    """Le righe attualmente nel DOM. Da rileggere a ogni passo di scorrimento."""
    grezze = await page.evaluate(_JS_RIGHE, quante)
    fuori = []
    for r in grezze:
        analizzata = analizza_riga_lista(r["testo"], lingua)
        non_letta = bool(r.get("nonLetta"))
        pallino = bool(r.get("pallinoConferma"))
        if non_letta != pallino:
            logger.debug(
                f"[InboxBrowser] segnale non-letta discordante alla riga {r['indice']}: "
                f"font-weight={non_letta} pallino={pallino} — si usa il font-weight"
            )
        fuori.append(RigaVisibile(
            indice=r["indice"], nome=analizzata.nome,
            ultimo_nostro=analizzata.ultimo_nostro, non_letta=non_letta,
            testo_grezzo=r["testo"],
        ))
    return fuori


async def apri_riga(page, indice: int, nome_atteso: str, lingua: str) -> str | None:
    """Apre la riga e ritorna lo username, oppure None se la verifica fallisce.

    La riga viene ri-risolta QUI, immediatamente prima del click: mai riusare un
    riferimento preso prima di una pausa.
    """
    handle = await page.evaluate_handle(
        """(idx) => [...document.querySelectorAll('div[role="button"], div[tabindex="0"], a')]
             .filter(e => { const r = e.getBoundingClientRect();
               return r.left < 660 && r.top > 150 && r.height > 50 && r.height < 130 && r.width > 250; })[idx] || null""",
        indice,
    )
    elemento = handle.as_element()
    if elemento is None:
        return None

    await human_input.human_click(page, elemento)
    await page.wait_for_timeout(1500)

    header = await page.evaluate(_JS_HEADER_THREAD)
    nome_trovato = header[0] if header else None
    if not nome_combacia(nome_atteso, nome_trovato):
        logger.warning(
            f"[InboxBrowser] verifica post-click fallita: atteso {nome_atteso!r}, "
            f"aperto {nome_trovato!r} — la lista si e' riordinata, riga non salvata"
        )
        return None

    href = await page.evaluate(_JS_HREF_THREAD)
    return estrai_username_thread(href, propri=set())


async def scorri(page) -> StatoScorrimento:
    """Un passo di scorrimento, sempre inferiore a una schermata."""
    frazione = random.uniform(PASSO_SCROLL_MIN, PASSO_SCROLL_MAX)
    await page.evaluate(
        """(f) => {
            let box = null, best = 0;
            for (const e of document.querySelectorAll('div')) {
                const r = e.getBoundingClientRect();
                if (r.left > 700 || r.width < 200 || r.height < 300) continue;
                if (!/auto|scroll/.test(getComputedStyle(e).overflowY)) continue;
                if (e.scrollHeight > best) { best = e.scrollHeight; box = e; }
            }
            if (box) box.scrollTop += box.clientHeight * f;
        }""",
        frazione,
    )
    stato = await page.evaluate(_JS_CONTENITORE)
    if stato is None:
        return StatoScorrimento(altezza=None, al_fondo=False)
    return StatoScorrimento(altezza=stato["altezza"], al_fondo=stato["alFondo"])


def decidi_da_segnali(
    altezza_prima: int | None, altezza_dopo: int | None,
    al_fondo: bool, falliti_inbox: int, attese_esaurite: bool,
) -> str:
    """'continua' | 'fine' | 'piantato'. Funzione pura: qui vive la decisione.

    Dichiarare "esaurita" una lista solo lenta fa perdere IN SILENZIO tutti i
    contatti che stavano sotto: nel dubbio si continua.
    """
    if altezza_prima is not None and altezza_dopo is not None and altezza_dopo > altezza_prima:
        return "continua"
    if not attese_esaurite:
        return "continua"
    if falliti_inbox > 0:
        return "piantato"
    return "fine" if al_fondo else "piantato"


async def decidi_fine_lista(page, falliti_inbox: list) -> str:
    """Un giro di scorrimento con attese a pazienza crescente: se l'altezza non
    cresce con NESSUna delle attese, solo allora si dichiarano esaurite (le
    attese sono qui, non nella funzione pura, perche' richiedono il browser).
    La decisione vera e propria resta in `decidi_da_segnali`."""
    stato = await scorri(page)
    altezza_prima = stato.altezza
    for attesa_s in ATTESE_S:
        await page.wait_for_timeout(int(attesa_s * 1000))
        stato = await scorri(page)
        if stato.altezza is not None and altezza_prima is not None and stato.altezza > altezza_prima:
            return "continua"
        altezza_prima = stato.altezza

    return decidi_da_segnali(
        altezza_prima=altezza_prima, altezza_dopo=stato.altezza,
        al_fondo=stato.al_fondo, falliti_inbox=len(falliti_inbox),
        attese_esaurite=True,
    )
