"""Sidebar di WhatsApp Web: righe e scorrimento. Le funzioni pure si testano
senza browser; scan_sidebar e scorri_sidebar (toccano `page`) si testano con
una pagina finta, sullo stesso modello di test_inbox_browser_pagina.py.
"""
import statistics

import pytest

from app.services.wa_discover.sidebar import (
    StatoScorrimento, piano_scroll_misurato, righe_dalla_sidebar, scan_sidebar,
    scorri_sidebar,
)


# ── piano_scroll_misurato: le misure sono vere, non modellate ─────────────
def test_il_gesto_rispetta_i_picchi_misurati():
    """Le misure vere (registra_scroll_umano.py, 1660 eventi, trackpad): picchi
    193-342 px per evento. Un gesto che non supera mai i 60 px e' il modello
    inventato del motore Instagram, ed e' 3-5 volte piu' lento del vero."""
    piano = piano_scroll_misurato(3000)
    picco = max(abs(px) for px, _ in piano)
    assert 100 < picco <= 342, f"picco {picco} fuori dal range misurato"


def test_il_gesto_copre_la_distanza_chiesta():
    piano = piano_scroll_misurato(3000)
    percorso = sum(px for px, _ in piano if px > 0)
    assert 2500 <= percorso <= 3500


def test_le_pause_sono_a_ritmo_di_frame():
    """16,7 ms mediani fra eventi: e' il ritmo che detta il browser, non una
    scelta. Pause da decine di ms sarebbero una firma."""
    piano = piano_scroll_misurato(3000)
    mediana = statistics.median(p for _, p in piano)
    assert 0.008 <= mediana <= 0.040


def test_due_gesti_non_sono_identici():
    a = piano_scroll_misurato(3000)
    b = piano_scroll_misurato(3000)
    assert a != b


# ── righe_dalla_sidebar: filtro puro sulle righe grezze ────────────────────
def test_scarta_le_righe_senza_titolo():
    """La lista virtualizzata tiene nel DOM placeholder senza testo."""
    grezze = [{"titolo": "", "top": 200}, {"titolo": "Fulvio", "top": 260}]
    assert [r["titolo"] for r in righe_dalla_sidebar(grezze, altezza_viewport=800)] == ["Fulvio"]


def test_scarta_le_righe_sotto_la_piega():
    """Misurato sul motore Instagram: righe a top=1473 con finestra alta 660
    passavano il filtro e arrivavano fino all'apertura, che le buttava con
    'nome mancante' -- rumore che nascondeva i fallimenti veri."""
    grezze = [{"titolo": "Fulvio", "top": 1473}, {"titolo": "Mamma", "top": 300}]
    assert [r["titolo"] for r in righe_dalla_sidebar(grezze, altezza_viewport=660)] == ["Mamma"]


# ── scan_sidebar / scorri_sidebar: toccano `page`, si testano con una pagina
# finta. Le query JS si riconoscono dal contenuto dello script, come in
# test_inbox_browser_pagina.py (_e_query_righe/_e_query_href).
def _e_query_scan(script: str) -> bool:
    return "righe:" in script


def _e_query_stato_pane(script: str) -> bool:
    return "alFondo" in script


class _FakePageSidebar:
    """Simula #pane-side. Risponde in modo diverso a seconda della query JS
    (scan delle righe vs stato/geometria del contenitore), cosi' scan_sidebar
    e scorri_sidebar non si confondono a vicenda -- stesso principio di
    _FakePage in test_inbox_browser_pagina.py."""

    class _Mouse:
        def __init__(self, outer):
            self._outer = outer

        async def move(self, x, y, steps=1):
            self._outer.mosso = True

        async def wheel(self, dx, dy):
            self._outer.wheel_eventi.append(dy)

    def __init__(self, righe=None, rowcount=291, viewport_h=800, pane_esiste=True,
                altezza=1200, al_fondo=False):
        self.righe = righe if righe is not None else []
        self.rowcount = rowcount
        self.viewport_h = viewport_h
        self.pane_esiste = pane_esiste
        self.altezza = altezza
        self.al_fondo = al_fondo
        self.geometria = {"left": 65, "top": 0, "w": 409, "h": 800, "clientHeight": 800}
        self.wheel_eventi = []
        self.mosso = False
        self.mouse = self._Mouse(self)

    async def evaluate(self, script, *args):
        if not self.pane_esiste:
            return None
        if _e_query_scan(script):
            return {"viewport_h": self.viewport_h, "righe": self.righe}
        if _e_query_stato_pane(script):
            return {"altezza": self.altezza, "alFondo": self.al_fondo,
                    "rowcount": self.rowcount, **self.geometria}
        raise AssertionError(f"query JS non riconosciuta dal fake: {script[:60]!r}")

    async def wait_for_timeout(self, ms):
        return None


@pytest.mark.asyncio
async def test_scan_sidebar_marca_le_righe_il_cui_titolo_e_un_numero():
    """La scan deve dire al chiamante quali righe hanno gia' il numero nel
    titolo (39% su Primero, PoC-5): sono quelle per cui NON serve aprire il
    pannello info e i 5,3s che costa (Task 5)."""
    page = _FakePageSidebar(righe=[
        {"position": 0, "titolo": "+39 342 146 0077", "top": 200},
        {"position": 1, "titolo": "Fulvio", "top": 260},
    ])
    righe = await scan_sidebar(page)
    assert [r["titolo"] for r in righe] == ["+39 342 146 0077", "Fulvio"]
    assert [r["titolo_e_numero"] for r in righe] == [True, False]


@pytest.mark.asyncio
async def test_scan_sidebar_applica_il_filtro_di_righe_dalla_sidebar():
    """Righe senza titolo o sotto la piega non devono arrivare al chiamante:
    scan_sidebar deve passare dal filtro puro, non reinventarlo."""
    page = _FakePageSidebar(righe=[
        {"position": 0, "titolo": "", "top": 200},
        {"position": 1, "titolo": "Fulvio", "top": 1473},
        {"position": 2, "titolo": "Mamma", "top": 300},
    ], viewport_h=800)
    righe = await scan_sidebar(page)
    assert [r["titolo"] for r in righe] == ["Mamma"]


@pytest.mark.asyncio
async def test_scan_sidebar_senza_pane_side_non_solleva_e_ritorna_vuoto():
    """#pane-side manca: comportamento esplicito (log + lista vuota), non un
    tentativo di indovinare un altro contenitore."""
    page = _FakePageSidebar(pane_esiste=False)
    righe = await scan_sidebar(page)
    assert righe == []


@pytest.mark.asyncio
async def test_scorri_sidebar_ritorna_lo_stato_con_rowcount():
    """aria-rowcount e' il totale dichiarato da WhatsApp: serve al chiamante
    (Task 7) per confrontarlo col raccolto e scoprire una raccolta parziale."""
    page = _FakePageSidebar(altezza=1800, al_fondo=False, rowcount=291)
    stato = await scorri_sidebar(page)
    assert stato == StatoScorrimento(altezza=1800, al_fondo=False, rowcount=291)


@pytest.mark.asyncio
async def test_scorri_sidebar_scorre_con_la_rotella_non_con_scrollTop():
    """Stesso motivo di inbox_browser.pagina.scorri: un salto istantaneo e'
    una firma che nessun dito produce."""
    page = _FakePageSidebar()
    await scorri_sidebar(page)
    assert page.mosso is True
    assert len(page.wheel_eventi) > 0


@pytest.mark.asyncio
async def test_scorri_sidebar_senza_pane_side_non_scorre():
    """#pane-side manca: nessuno scorrimento, nessun tentativo su un altro
    elemento -- comportamento esplicito, non una scommessa."""
    page = _FakePageSidebar(pane_esiste=False)
    stato = await scorri_sidebar(page)
    assert stato == StatoScorrimento(altezza=None, al_fondo=False, rowcount=None)
    assert page.wheel_eventi == []
    assert page.mosso is False


@pytest.mark.asyncio
async def test_totale_dichiarato_si_legge_senza_scorrere():
    """Il totale deve essere leggibile PRIMA di muovere qualcosa.

    Se l'unico modo di conoscerlo fosse chiamare scorri_sidebar, per misurare lo
    stato bisognerebbe modificarlo. E il totale serve proprio all'inizio: e' il
    termine di paragone con cui, a fine scan, ci si accorge di una raccolta
    parziale invece di chiuderla come "completata".
    """
    from app.services.wa_discover.sidebar import totale_dichiarato

    pagina = _FakePageSidebar(rowcount=291)          # il totale vero di Primero
    assert await totale_dichiarato(pagina) == 291
    assert pagina.wheel_eventi == [], "non deve aver scorso nulla"


@pytest.mark.asyncio
async def test_totale_dichiarato_ignoto_non_e_zero():
    """None significa "non lo so", e chi legge non deve confonderlo con "nessuna
    chat": scambiarli farebbe dichiarare completa una lista mai letta."""
    from app.services.wa_discover.sidebar import totale_dichiarato

    assert await totale_dichiarato(_FakePageSidebar(pane_esiste=False)) is None
