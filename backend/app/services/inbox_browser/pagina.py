"""Interazione col DOM dell'inbox web.

REGOLA DI DISEGNO (dal collaudo del 10/08): il JS RACCOGLIE, Python DECIDE.
Nessuna coordinata assoluta va cablata dentro una query JS. Le soglie `left>660`
e `left>700` della prima versione non esistevano nel layout reale e rompevano
tre cose in silenzio — nome del thread mai trovato, username mai trovato, e la
conversazione scrollata al posto della lista. Il confine fra la colonna della
lista e quella del thread si MISURA a runtime (`bordo_colonne`), e le decisioni
che ne dipendono stanno in funzioni pure, coperte da test con le coordinate
vere: vedi tests/test_inbox_browser_geometria.py.

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

import math
import random
from collections import Counter
from dataclasses import dataclass

from loguru import logger

from app.browser import human_input
from app.services.inbox_browser.testo import (
    analizza_riga_lista, estrai_username_thread, normalizza_nome,
)

# Il passo non e' piu' limitato dal buffer: `scorri_leggendo` campiona durante
# il gesto (vedi PX_FRA_LETTURE), quindi nessuna riga passa senza essere vista.
# Un gesto umano misurato copre da 6 a 25 schermate; qui si resta a 2-4, che
# gia' dimezza il numero di giri e quindi le righe riesaminate (Task 2).
# USATE SOLO da `scorri_leggendo`: `scorri` (senza campionamento) ha le sue
# costanti separate qui sotto apposta, per non far ereditare un passo grande
# senza lettura a chi la chiama ancora direttamente (scripts diagnostici, fra
# cui l'harness di misura del Task 12 — che in Fase B scrive su un account di
# produzione: un passo grande e cieco li' significherebbe perdere contatti
# veri in silenzio, esattamente il difetto che questo task elimina altrove).
PASSO_SCROLL_MIN = 2.0
PASSO_SCROLL_MAX = 4.0

# Sotto una schermata: senza campionamento, sopra il buffer renderizzato le
# righe si perdono in silenzio. Usate solo da `scorri`.
PASSO_SCROLL_SENZA_LETTURA_MIN = 0.6
PASSO_SCROLL_SENZA_LETTURA_MAX = 0.8

# Il nome del thread sta nella parte SINISTRA della colonna di destra (misurato:
# x=544 con bordo a 471 e finestra larga 1280). Oltre questa frazione ci sono i
# controlli in alto a destra — 'Top' a x=1197 e' quello che il motore scambiava
# per il nome della persona.
FRAZIONE_FASCIA_NOME = 0.6

# Quanto in alto puo' stare il nome nell'header del thread.
ALTEZZA_HEADER_PX = 130

# Il contenitore della lista puo' sforare di poco il bordo misurato (bordi,
# ombre, scrollbar): oltre questo margine e' un'altra colonna.
TOLLERANZA_BORDO_PX = 30

# Scatti di rotella. Un dito su un trackpad non produce otto colpi da cento
# pixel: produce decine di eventi piccoli e ravvicinati, che accelerano e poi
# frenano. Sopra i 60px per evento la scalinata si vede a occhio.
SCATTO_MAX_PX = 60
PAUSA_SCATTO_MIN_S = 0.012
PAUSA_SCATTO_MAX_S = 0.045

# Quanti pixel per scatto, in media: da qui esce il numero di eventi.
PX_PER_SCATTO_MIN = 18
PX_PER_SCATTO_MAX = 32
SCATTI_MIN = 8
SCATTI_MAX = 45

PROB_RIMBALZO = 0.12

# Attese a pazienza crescente prima di dichiarare qualcosa sulla fine lista.
ATTESE_S = (1, 2, 4, 8, 16)

# Attese a pazienza crescente per il pannello del thread dopo il click (vedi
# apri_riga). Totale 3s: il caso rapido esce all'attesa piu' corta.
_ATTESE_HEADER_S = (0.5, 1.0, 1.5)

_JS_RIGHE = """(nRighe) => {
    const righe = [...document.querySelectorAll('div[role="button"], div[tabindex="0"], a')]
      .filter(e => { const r = e.getBoundingClientRect();
        return r.top > 150 && r.height > 50 && r.height < 130 && r.width > 250; });

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

    const viewport = {w: window.innerWidth, h: window.innerHeight};
    return {viewport, righe: righe.slice(0, nRighe).map((e, i) => {
        const r = e.getBoundingClientRect();
        return {indice: i, testo: e.innerText,
                left: Math.round(r.left), right: Math.round(r.right),
                top: Math.round(r.top), w: Math.round(r.width),
                nonLetta: fontWeightAlto(e),
                pallinoConferma: pallinoPresente(e)};
    })};
}"""

# TUTTI i contenitori scrollabili, in ordine di documento: quale sia quello
# della lista lo decide `scegli_contenitore` in Python, col bordo misurato.
# L'ordine e' la chiave con cui poi si ritrova lo stesso elemento (vedi
# _JS_STATO_CONTENITORE), quindi le due query devono restare identiche.
_JS_CANDIDATI = """() => [...document.querySelectorAll('div')].map(e => {
    if (!/auto|scroll/.test(getComputedStyle(e).overflowY)) return null;
    if (e.scrollHeight <= e.clientHeight + 10) return null;
    const r = e.getBoundingClientRect();
    if (r.height < 100) return null;
    return {left: Math.round(r.left), right: Math.round(r.right), top: Math.round(r.top),
            w: Math.round(r.width), h: Math.round(r.height),
            scrollHeight: e.scrollHeight, clientHeight: e.clientHeight,
            scrollTop: Math.round(e.scrollTop)};
}).filter(Boolean)"""

_JS_STATO_CONTENITORE = """(idx) => {
    const box = [...document.querySelectorAll('div')].filter(e => {
        if (!/auto|scroll/.test(getComputedStyle(e).overflowY)) return false;
        if (e.scrollHeight <= e.clientHeight + 10) return false;
        return e.getBoundingClientRect().height >= 100;
    })[idx];
    if (!box) return null;
    return {altezza: box.scrollHeight, top: Math.round(box.scrollTop),
            visibile: box.clientHeight,
            alFondo: (box.scrollHeight - box.scrollTop - box.clientHeight) < 50};
}"""

# Href con la loro X: quali appartengano al thread lo decide `href_thread`.
_JS_HREF = """() => [...document.querySelectorAll('a[href^="/"]')].map(e => {
    const r = e.getBoundingClientRect();
    return {left: Math.round(r.left), top: Math.round(r.top), href: e.getAttribute('href')};
})"""

# Nodi di testo della fascia alta, con la loro posizione: quale sia il nome del
# thread lo decide `nome_header`.
_JS_FASCIA_ALTA = """() => [...document.querySelectorAll('span, div, h1, h2')]
    .filter(e => { const r = e.getBoundingClientRect();
      return e.children.length === 0 && (e.textContent || '').trim().length > 1
             && r.top < 200 && r.width > 0 && r.height > 0; })
    .map(e => { const r = e.getBoundingClientRect();
      return {left: Math.round(r.left), top: Math.round(r.top),
              testo: e.textContent.trim()}; })"""


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


def _destro(elemento: dict) -> float:
    """Il margine destro, anche quando il DOM ha dato solo left+larghezza."""
    destro = elemento.get("right")
    if destro is not None:
        return float(destro)
    return float(elemento.get("left", 0)) + float(elemento.get("w", 0))


def bordo_colonne(righe: list[dict]) -> float | None:
    """Il confine fra colonna della lista e colonna del thread, in pixel.

    Si deduce dalle righe stesse: la lista chat finisce dove finiscono le sue
    righe. Vince il margine destro RICORRENTE, non il massimo: le righe di una
    lista sono tutte larghe uguale, mentre un elemento della colonna del thread
    che sfuggisse al filtro sposterebbe il bordo a destra e farebbe tornare
    vuoti header e href — cioe' raccolta zero, in silenzio.

    None se non c'e' nessuna riga da cui misurare: li' non si tira a indovinare,
    si rinuncia (leggere l'header sbagliato non da' errore, da' dati attribuiti
    alla persona sbagliata).
    """
    destri = [_destro(r) for r in righe or [] if r.get("right") or r.get("w")]
    if not destri:
        return None
    return Counter(destri).most_common(1)[0][0]


def righe_valide(grezze: list[dict], altezza_viewport: int,
                 bordo: float | None = None) -> list[dict]:
    """Solo le righe che sono davvero chat visibili della lista.

    Tre scarti, tutti visti dal vivo:
    - placeholder della lista virtualizzata, senza testo;
    - righe molto sotto la piega (misurato: top=1473 con finestra alta 660):
      passavano il filtro e arrivavano fino ad `apri_riga`, che le buttava con
      "nome_atteso mancante" — rumore che nascondeva i fallimenti veri;
    - elementi che non appartengono alla colonna della lista, quando il bordo
      e' noto.
    """
    fuori = []
    for r in grezze or []:
        if not (r.get("testo") or "").strip():
            continue
        if float(r.get("top", 0)) >= altezza_viewport:
            continue
        if bordo is not None and _destro(r) > bordo + TOLLERANZA_BORDO_PX:
            continue
        fuori.append(r)
    return fuori


def nome_header(nodi: list[dict], bordo: float | None, larghezza_viewport: int) -> str | None:
    """Il nome della persona nell'header del thread aperto.

    Candidati: i nodi a destra del bordo ma nella prima parte della colonna
    (dove Instagram scrive nome e username), e in alto. Fra questi vince il piu'
    in alto — misurato: nome a y=18, username a y=41.
    """
    if bordo is None:
        return None
    limite_destro = bordo + (larghezza_viewport - bordo) * FRAZIONE_FASCIA_NOME
    candidati = [
        n for n in nodi or []
        # Il limite INFERIORE conta quanto quello superiore: nel pannello
        # restano nodi con top negativo — messaggi vecchi scrollati sopra il
        # bordo della finestra. Ordinando per top crescente vincerebbero sempre
        # loro (misurato: -185 contro 18, e il nome diventava una data).
        if bordo <= float(n.get("left", 0)) <= limite_destro
        and 0 <= float(n.get("top", 0)) < ALTEZZA_HEADER_PX
        and (n.get("testo") or "").strip()
    ]
    if not candidati:
        return None
    candidati.sort(key=lambda n: (float(n["top"]), float(n["left"])))
    return candidati[0]["testo"]


def username_header(nodi: list[dict], nome: str | None, bordo: float | None,
                    larghezza_viewport: int) -> str | None:
    """Lo username scritto SOTTO il nome, nell'header del thread aperto.

    Serve quando gli href del pannello sono ambigui: una conversazione che
    contiene un post condiviso porta il link di un altro profilo, e
    `estrai_username_thread` — giustamente — preferisce scartare piuttosto che
    tirare a indovinare. Ma l'informazione giusta e' li', ventitre' pixel sotto
    il nome (misurato: nome a y=18, username a y=41).

    Chi chiama deve comunque confermarlo contro gli href veri: qui si estrae un
    candidato, non si stabilisce una verita'.
    """
    if bordo is None or not nome:
        return None
    nome_normale = normalizza_nome(nome)
    limite_destro = bordo + (larghezza_viewport - bordo) * FRAZIONE_FASCIA_NOME
    fascia = sorted(
        (n for n in nodi or []
         if bordo <= float(n.get("left", 0)) <= limite_destro
         and 0 <= float(n.get("top", 0)) < ALTEZZA_HEADER_PX
         and (n.get("testo") or "").strip()),
        key=lambda n: (float(n["top"]), float(n["left"])),
    )
    for i, nodo in enumerate(fascia):
        if normalizza_nome(nodo["testo"]) != nome_normale:
            continue
        for successivo in fascia[i + 1:]:
            candidato = (successivo.get("testo") or "").strip()
            # Uno username Instagram non contiene spazi: e' cio' che distingue
            # 'ylianpaventi_' da 'Attivo 3 ore fa', che occupa lo stesso posto.
            if candidato and " " not in candidato:
                return candidato.lower()
            return None
        return None
    return None


def href_thread(href: list[dict], bordo: float | None) -> list[str]:
    """Gli href della colonna del thread: gli unici che possono contenere lo
    username dell'interlocutore. Quelli della barra laterale (fra cui il link al
    proprio profilo) restano fuori."""
    if bordo is None:
        return []
    return [h["href"] for h in href or [] if float(h.get("left", 0)) >= bordo]


def scegli_contenitore(candidati: list[dict], bordo: float | None) -> int | None:
    """L'indice del contenitore scrollabile della LISTA, o None.

    Non si prende "quello con lo scrollHeight piu' grande": a chat aperta il
    pannello della conversazione ne ha uno maggiore (misurato: 1360 contro
    1224), e scrollare quello lascia la lista ferma per sempre senza sollevare
    nessun errore. Prima si tengono solo i contenitori che stanno DENTRO la
    colonna della lista, poi fra quelli vince il piu' alto.
    """
    if bordo is None:
        return None
    dentro = [
        (i, c) for i, c in enumerate(candidati or [])
        if _destro(c) <= bordo + TOLLERANZA_BORDO_PX
    ]
    if not dentro:
        return None
    return max(dentro, key=lambda coppia: coppia[1].get("scrollHeight", 0))[0]


def piano_scroll(px_totali: int) -> list[tuple[int, float]]:
    """Il gesto di scorrimento, come sequenza di (pixel, pausa in secondi).

    Un solo salto da mezza schermata e' la firma piu' riconoscibile che ci sia:
    nessun dito muove una lista in un evento solo. Qui il gesto si scompone in
    scatti piccoli e disuguali, con pause brevi, e ogni tanto un rimbalzo
    all'indietro — quello che fa una mano quando supera il punto che voleva.
    """
    if px_totali <= 0:
        return []

    quanti = round(px_totali / random.uniform(PX_PER_SCATTO_MIN, PX_PER_SCATTO_MAX))
    quanti = max(SCATTI_MIN, min(SCATTI_MAX, quanti))

    # Profilo a campana: il gesto parte piano, prende velocita' a meta' e frena
    # verso la fine. E' la firma di un movimento vero — una sequenza di scatti
    # tutti uguali e' un motore, non una mano.
    pesi = [math.sin(math.pi * (i + 0.5) / quanti) for i in range(quanti)]
    totale_pesi = sum(pesi) or 1.0

    piano: list[tuple[int, float]] = []
    for peso in pesi:
        delta = px_totali * peso / totale_pesi * random.uniform(0.82, 1.18)
        delta = max(3, min(SCATTO_MAX_PX, round(delta)))
        piano.append((delta, random.uniform(PAUSA_SCATTO_MIN_S, PAUSA_SCATTO_MAX_S)))

    # Ogni tanto la mano supera il punto e torna indietro di qualche pixel.
    if random.random() < PROB_RIMBALZO and len(piano) > 3:
        dove = random.randint(len(piano) // 2, len(piano) - 1)
        piano.insert(dove, (-random.randint(8, 30),
                            random.uniform(PAUSA_SCATTO_MIN_S, PAUSA_SCATTO_MAX_S)))

    return piano


async def _leggi_righe_grezze(page, quante: int = 30) -> tuple[list[dict], dict, float | None]:
    """Righe della lista, viewport e bordo fra le colonne, in una sola lettura.

    Il bordo si misura QUI e viene passato a valle: ricalcolarlo dopo il click
    significherebbe misurarlo su un DOM gia' cambiato.
    """
    dati = await page.evaluate(_JS_RIGHE, quante) or {}
    viewport = dati.get("viewport") or {"w": 0, "h": 0}
    grezze = dati.get("righe") or []
    bordo = bordo_colonne(grezze)
    return righe_valide(grezze, viewport.get("h", 0), bordo), viewport, bordo


async def leggi_righe_visibili(page, lingua: str, quante: int = 30) -> list[RigaVisibile]:
    """Le righe attualmente nel DOM. Da rileggere a ogni passo di scorrimento."""
    grezze, _viewport, _bordo = await _leggi_righe_grezze(page, quante)
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


async def apri_riga(
    page, indice: int, nome_atteso: str, lingua: str, account_username: str | None = None,
) -> str | None:
    """Apre la riga e ritorna lo username, oppure None se la verifica fallisce.

    La riga viene ri-risolta QUI, immediatamente prima del click — ma per
    CONTENUTO (nome), non per indice: `human_click` fa `scroll_into_view_if_needed`
    prima di ogni click (human_input.py:96-98), quindi ogni apertura scorre la
    pagina. In un lotto letto in un colpo solo, buona parte delle righe sta sotto
    la piega iniziale; quando il ciclo le raggiunge, l'indice geometrico di
    QUELLA riga puo' essere cambiato per via degli scroll precedenti (lista
    virtualizzata, vedi modulo docstring punto 1). Interrogare di nuovo il DOM
    con lo stesso indice rischierebbe di risolvere un elemento diverso da quello
    inteso. `indice` resta in firma solo come hint diagnostico nei log, non come
    fonte di verita': se il nome non si trova fra le righe attualmente visibili,
    si rinuncia senza cliccare — la riga rimane "non riconosciuta" e verra'
    ritentata al giro successivo, esattamente come un mismatch post-click.

    `lingua` e' accettata per uniformita' di firma con `leggi_righe_visibili`
    (il chiamante ha gia' la lingua a portata di mano per quella chiamata), ma
    qui non serve: la verifica post-click confronta i nomi via `nome_combacia`,
    che normalizza senza dipendere dalla lingua dell'interfaccia.

    `account_username`, se noto, esclude il proprio profilo dai candidati href
    del thread (vedi `estrai_username_thread`): senza di esso un link al proprio
    avatar/header nel pannello produce 2 candidati e la riga viene scartata
    anche se il click era corretto.
    """
    nome_target = normalizza_nome(nome_atteso)
    if not nome_target:
        logger.warning(f"[InboxBrowser] apri_riga: nome_atteso mancante (indice hint {indice}) — nessun click")
        return None

    righe, viewport, bordo = await _leggi_righe_grezze(page)
    indice_effettivo = None
    for r in righe:
        prima_riga = (r.get("testo") or "").split("\n", 1)[0].strip()
        if prima_riga and normalizza_nome(prima_riga) == nome_target:
            indice_effettivo = r["indice"]
            break

    if indice_effettivo is None:
        logger.warning(
            f"[InboxBrowser] riga non trovata al momento del click per {nome_atteso!r} "
            f"(indice hint {indice}) — probabilmente scorsa fuori dal DOM virtualizzato"
        )
        return None

    handle = await page.evaluate_handle(
        """(idx) => [...document.querySelectorAll('div[role="button"], div[tabindex="0"], a')]
             .filter(e => { const r = e.getBoundingClientRect();
               return r.top > 150 && r.height > 50 && r.height < 130 && r.width > 250; })[idx] || null""",
        indice_effettivo,
    )
    elemento = handle.as_element()
    if elemento is None:
        return None

    await human_input.human_click(page, elemento)

    # Pazienza crescente (stesso principio di decidi_fine_lista): un'attesa fissa
    # di 1.5s bastava sulla latenza misurata nel Task 0, ma sul proxy reale
    # dell'account misurato in QA (Task 15) il pannello del thread arriva spesso
    # fra 1 e 2s — con l'attesa fissa quel margine stretto faceva scartare come
    # "lista riordinata" molte righe in realta' corrette (0-3/5 aperture riuscite
    # a seconda del giro). Si esce appena l'header compare, quindi il caso rapido
    # resta rapido; il caso lento ora ha 3s invece di 1.5s prima di arrendersi.
    # Il pannello non si svuota nell'istante del click: per qualche decimo di
    # secondo l'header e' ancora quello della chat precedente, e leggerlo li'
    # significa confrontare la riga cliccata con la persona sbagliata — visto
    # dal vivo il 10/08 aprendo tre chat di fila (letti 'Verificato' e lo
    # username di un'altra conversazione, tutte righe buone buttate). L'URL
    # /direct/t/<id>/ cambia solo a transizione finita, quindi si aspetta
    # quello. Se non cambia (stessa chat gia' aperta) si legge comunque: la
    # verifica del nome resta l'ultima parola.
    url_prima = getattr(page, "url", None)
    nome_trovato = None
    thread_cambiato = url_prima is None
    for attesa_s in _ATTESE_HEADER_S:
        await page.wait_for_timeout(int(attesa_s * 1000))
        if not thread_cambiato and getattr(page, "url", None) != url_prima:
            thread_cambiato = True
        nodi = await page.evaluate(_JS_FASCIA_ALTA)
        candidato = nome_header(nodi, bordo, viewport.get("w", 0))
        if candidato and thread_cambiato:
            nome_trovato = candidato
            break
        nome_trovato = candidato or nome_trovato

    if not nome_combacia(nome_atteso, nome_trovato):
        logger.warning(
            f"[InboxBrowser] verifica post-click fallita: atteso {nome_atteso!r}, "
            f"aperto {nome_trovato!r} (bordo colonne {bordo}) — riga non salvata"
        )
        return None

    href = href_thread(await page.evaluate(_JS_HREF), bordo)
    propri = {account_username} if account_username else set()
    username = estrai_username_thread(href, propri=propri)
    if username:
        return username

    # Href ambigui: succede quando la conversazione contiene un post condiviso
    # e porta con se' il link di un altro profilo (misurato l'11/08 su 'Ylian
    # Paventi', con dentro un post di @outpump). Lo username giusto e' scritto
    # sotto il nome nell'header, ma lo si accetta solo se un link a QUEL
    # profilo esiste davvero nel pannello: due indizi indipendenti, non uno.
    candidato = username_header(nodi, nome_trovato, bordo, viewport.get("w", 0))
    if not candidato:
        return None
    segmenti = {s.strip("/").lower() for s in href if s.count("/") <= 2}
    if candidato in segmenti and candidato not in {
        (account_username or "").lower().lstrip("@")
    }:
        logger.info(
            f"[InboxBrowser] href ambigui per {nome_trovato!r}: username preso "
            f"dall'header e confermato dai link ({candidato})"
        )
        return candidato
    return None


# Ogni quanti pixel di scorrimento si rilegge la lista. Un terzo del buffer
# renderizzato (~13 righe da 72px con finestra alta 940): abbastanza fitto da
# non lasciar passare nessuna riga, abbastanza raro da non pesare. Leggere costa
# lo 0.06% del tempo di sessione (misurato l'11/08: 1.0s su 1727).
PX_FRA_LETTURE = 250


async def scorri_leggendo(page, lingua: str, su_righe) -> StatoScorrimento:
    """Un gesto di scorrimento che campiona la lista MENTRE si muove.

    Il limite di 0.6-0.8 schermate per gesto non veniva da Instagram: veniva
    dal fatto che le righe si leggevano solo a gesto finito, e quelle
    attraversate nel mezzo non erano piu' nel DOM. Campionando ogni
    PX_FRA_LETTURE pixel il limite cade, e il gesto puo' andare alla velocita'
    misurata su una mano vera.

    `su_righe` viene chiamata con le righe viste a ogni campionamento; spetta
    al chiamante deduplicare (il motore lo fa con la memoria di sessione). Il
    primo campionamento avviene PRIMA di muovere la rotella: la funzione non fa
    affidamento su una lettura precedente del chiamante, altrimenti le righe
    visibili nella posizione di partenza sfuggirebbero a chi la chiama isolata.
    """
    await su_righe(await leggi_righe_visibili(page, lingua))

    _righe, _viewport, bordo = await _leggi_righe_grezze(page)
    candidati = await page.evaluate(_JS_CANDIDATI)
    indice = scegli_contenitore(candidati, bordo)
    if indice is None:
        logger.warning(
            f"[InboxBrowser] contenitore della lista non riconosciuto "
            f"(bordo {bordo}) — nessuno scorrimento"
        )
        return StatoScorrimento(altezza=None, al_fondo=False)

    box = candidati[indice]
    frazione = random.uniform(PASSO_SCROLL_MIN, PASSO_SCROLL_MAX)
    px = int(box["clientHeight"] * frazione)

    x = box["left"] + box["w"] * random.uniform(0.3, 0.7)
    y = box["top"] + box["h"] * random.uniform(0.3, 0.7)
    await page.mouse.move(x, y, steps=random.randint(5, 15))

    percorso_da_ultima_lettura = 0
    for delta, pausa in piano_scroll(px):
        await page.mouse.wheel(0, delta)
        await page.wait_for_timeout(int(pausa * 1000))
        percorso_da_ultima_lettura += abs(delta)
        if percorso_da_ultima_lettura >= PX_FRA_LETTURE:
            percorso_da_ultima_lettura = 0
            await su_righe(await leggi_righe_visibili(page, lingua))

    await su_righe(await leggi_righe_visibili(page, lingua))

    stato = await page.evaluate(_JS_STATO_CONTENITORE, indice)
    if stato is None:
        return StatoScorrimento(altezza=None, al_fondo=False)
    return StatoScorrimento(altezza=stato["altezza"], al_fondo=stato["alFondo"])


async def scorri(page) -> StatoScorrimento:
    """Un passo di scorrimento SENZA campionamento delle righe.

    Il ciclo del motore non la usa piu' (vedi `decidi_fine_lista`, che scorre
    con `scorri_leggendo` per non perdere le righe attraversate): resta per gli
    script diagnostici (`scripts/supervisione_inbox_browser.py`,
    `scripts/probe_inbox_web_apri_riga.py`) che la chiamano direttamente e non
    hanno bisogno di raccogliere righe, solo di far avanzare la lista.
    """
    _righe, _viewport, bordo = await _leggi_righe_grezze(page)
    candidati = await page.evaluate(_JS_CANDIDATI)
    indice = scegli_contenitore(candidati, bordo)
    if indice is None:
        logger.warning(
            f"[InboxBrowser] contenitore della lista non riconosciuto "
            f"(bordo {bordo}, {len(candidati or [])} candidati) — nessuno scorrimento"
        )
        return StatoScorrimento(altezza=None, al_fondo=False)

    box = candidati[indice]
    frazione = random.uniform(PASSO_SCROLL_SENZA_LETTURA_MIN, PASSO_SCROLL_SENZA_LETTURA_MAX)
    px = int(box["clientHeight"] * frazione)

    x = box["left"] + box["w"] * random.uniform(0.3, 0.7)
    y = box["top"] + box["h"] * random.uniform(0.3, 0.7)
    await page.mouse.move(x, y, steps=random.randint(5, 15))

    for delta, pausa in piano_scroll(px):
        await page.mouse.wheel(0, delta)
        await page.wait_for_timeout(int(pausa * 1000))

    stato = await page.evaluate(_JS_STATO_CONTENITORE, indice)
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


async def decidi_fine_lista(page, falliti_inbox: list, lingua: str, su_righe) -> str:
    """Un giro di scorrimento con attese a pazienza crescente: se l'altezza non
    cresce con NESSUna delle attese, solo allora si dichiarano esaurite (le
    attese sono qui, non nella funzione pura, perche' richiedono il browser).
    La decisione vera e propria resta in `decidi_da_segnali`.

    `falliti_inbox` e' condiviso per l'intera sessione (30-55 min): si
    misura solo il DELTA da quando si e' entrati in questa chiamata, altrimenti
    un fallimento isolato all'inizio della sessione marcherebbe 'piantato'
    ogni fine-lista successiva per il resto della sessione.

    Lo scorrimento usa `scorri_leggendo`, non un `scorri` cieco: e' questa
    funzione che compie il vero avanzamento della lista nel ciclo del motore
    (il chiamante processa solo le righe che `su_righe` accumula), quindi e'
    qui che le righe attraversate andrebbero perse in silenzio se il gesto non
    campionasse durante il movimento. `lingua` e `su_righe` sono obbligatori
    apposta: un default silenzioso che dimentica la lettura sarebbe il tipo di
    errore che questa funzione esiste per evitare.
    """
    baseline_falliti = len(falliti_inbox)
    stato = await scorri_leggendo(page, lingua, su_righe)
    altezza_prima = stato.altezza
    for attesa_s in ATTESE_S:
        await page.wait_for_timeout(int(attesa_s * 1000))
        stato = await scorri_leggendo(page, lingua, su_righe)
        if stato.altezza is not None and altezza_prima is not None and stato.altezza > altezza_prima:
            return "continua"
        altezza_prima = stato.altezza

    return decidi_da_segnali(
        altezza_prima=altezza_prima, altezza_dopo=stato.altezza,
        al_fondo=stato.al_fondo, falliti_inbox=len(falliti_inbox) - baseline_falliti,
        attese_esaurite=True,
    )
