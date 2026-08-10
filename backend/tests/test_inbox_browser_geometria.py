"""Geometria del DOM dell'inbox web: le decisioni sono di Python, non del JS.

Il motore nasceva con tre soglie assolute cablate nelle query JS (`left < 660`
per le righe, `left > 660` per header e href, `left > 700` per il contenitore
scrollabile). Il collaudo dal vivo del 10/08 su @primero_adv3 ha mostrato che
quelle soglie non esistono nel layout reale, e che ognuna rompeva qualcosa:

- il nome dell'header sta a x=544 e lo username a x=488: con `left > 660` non
  venivano MAI trovati. `apri_riga` falliva ogni verifica post-click, e anche se
  fosse passata, `estrai_username_thread` riceveva una lista vuota. Nessuna riga
  poteva essere salvata, mai;
- con `left > 660` l'unico nodo catturato era il pulsante 'Top' a x=1197 —
  da cui il famoso "atteso 'ema', aperto 'Top'";
- il contenitore veniva scelto come "quello con lo scrollHeight piu' grande fra
  quelli con left <= 700": aperta la prima chat, il pannello della conversazione
  (left=488, scrollHeight=1360) superava la lista (left=72, scrollHeight=1224) e
  il motore scrollava la conversazione invece della lista. La lista non avanzava
  piu', l'altezza non cresceva, `decidi_fine_lista` dichiarava "piantato" e la
  campagna finiva in error con migliaia di chat mai viste;
- il filtro righe non aveva un limite superiore su `top`: catturava il
  placeholder della lista virtualizzata a top=1473 (viewport alto 660) con testo
  vuoto, che diventava il warning "nome_atteso mancante".

I numeri usati qui sono quelli MISURATI da
`scripts/probe_inbox_web_geometria.py` su @primero_adv3 il 10/08, viewport
1280x660. Non sono inventati e non vanno "arrotondati" se un test diventa
scomodo: sono la fotografia del layout che il motore deve saper leggere.
"""
import pytest

from app.services.inbox_browser.pagina import (
    bordo_colonne, href_thread, nome_header, piano_scroll, righe_valide,
    scegli_contenitore,
)

VIEWPORT_W = 1280
VIEWPORT_H = 660

# Righe della lista chat, misurate. L'ultima e' il placeholder virtualizzato:
# fuori dalla finestra (top 1473 > 660) e senza testo.
RIGHE_MISURATE = [
    {"left": 72, "right": 471, "top": 321, "testo": "ema\n3515614757"},
    {"left": 72, "right": 471, "top": 393, "testo": "Rita\nTu: Vorresti passare 3 giorni"},
    {"left": 72, "right": 471, "top": 465, "testo": "NIHIL\n2 new messages"},
    {"left": 72, "right": 471, "top": 537, "testo": "Massimiliano Bello\nTu: Vorresti"},
    {"left": 72, "right": 471, "top": 609, "testo": "FRA\nTu: Vorresti passare una vacanza"},
    {"left": 72, "right": 471, "top": 1473, "testo": ""},
]

# Nodi foglia della fascia alta dopo aver aperto la chat di 'ema', in ordine di
# documento: la barra sinistra, i tab della lista, poi il pannello del thread.
FASCIA_ALTA_MISURATA = [
    {"left": 96, "top": 38, "testo": "primero_adv3"},
    {"left": 114, "top": 86, "testo": "Primary"},
    {"left": 247, "top": 86, "testo": "General"},
    {"left": 376, "top": 86, "testo": "Richieste"},
    {"left": 544, "top": 18, "testo": "ema"},
    {"left": 544, "top": 41, "testo": "emanuele_zerbi_"},
    {"left": 1197, "top": 80, "testo": "Top"},
    {"left": 488, "top": 175, "testo": "26/06/25, 15:56"},
]

HREF_MISURATI = [
    {"left": 12, "top": 130, "href": "/reels/"},
    {"left": 12, "top": 242, "href": "/explore/"},
    {"left": 12, "top": 466, "href": "/primero_adv3/"},
    {"left": 488, "top": 16, "href": "/emanuele_zerbi_/"},
    {"left": 488, "top": 399, "href": "/emanuele_zerbi_"},
    {"left": 488, "top": 540, "href": "/emanuele_zerbi_"},
]

# Contenitori scrollabili: la lista chat e, a chat aperta, la conversazione.
LISTA_CHAT = {"left": 72, "right": 471, "w": 399, "h": 339,
              "scrollHeight": 1224, "clientHeight": 339}
PANNELLO_CONVERSAZIONE = {"left": 488, "right": 1280, "w": 792, "h": 506,
                          "scrollHeight": 1360, "clientHeight": 506}


# ── il bordo fra le due colonne si deduce, non si cabla ────────────────────
def test_il_bordo_e_il_margine_destro_delle_righe():
    assert bordo_colonne(RIGHE_MISURATE) == 471


def test_il_bordo_ignora_un_elemento_estraneo_della_colonna_destra():
    """Il bordo regge TUTTO il resto: header, href, scelta del contenitore. Se
    un solo elemento della colonna del thread finisse fra le righe, il bordo
    schizzerebbe a destra, header e href diventerebbero vuoti e la raccolta
    tornerebbe zero senza un errore. Vince il margine RICORRENTE, non il piu'
    grande: le righe di una lista sono tutte larghe uguale, un intruso no."""
    intruso = {"left": 500, "right": 1200, "top": 400, "testo": "messaggio lungo nel thread"}
    assert bordo_colonne(RIGHE_MISURATE + [intruso]) == 471


def test_l_elemento_estraneo_non_e_una_riga_della_lista():
    intruso = {"left": 500, "right": 1200, "top": 400, "testo": "messaggio lungo nel thread"}
    valide = righe_valide(RIGHE_MISURATE + [intruso], altezza_viewport=VIEWPORT_H, bordo=471)
    assert all(r["right"] == 471 for r in valide)


def test_senza_righe_non_si_inventa_un_bordo():
    """Nessuna riga = nessuna misura. Tirare a indovinare un bordo qui
    significherebbe leggere l'header sbagliato senza accorgersene."""
    assert bordo_colonne([]) is None


# ── righe: il placeholder virtualizzato non e' una chat ────────────────────
def test_la_riga_fuori_dalla_finestra_viene_scartata():
    valide = righe_valide(RIGHE_MISURATE, altezza_viewport=VIEWPORT_H)
    assert [r["top"] for r in valide] == [321, 393, 465, 537, 609]


def test_la_riga_senza_testo_viene_scartata_anche_se_dentro_la_finestra():
    grezze = [{"left": 72, "right": 471, "top": 400, "testo": "   "}]
    assert righe_valide(grezze, altezza_viewport=VIEWPORT_H) == []


# ── header del thread: il nome sta a 544, non oltre 660 ────────────────────
def test_il_nome_del_thread_si_legge_a_destra_del_bordo():
    """Il bug originale in una riga: con la soglia 660 questo tornava 'Top'."""
    assert nome_header(FASCIA_ALTA_MISURATA, bordo=471,
                       larghezza_viewport=VIEWPORT_W) == "ema"


def test_i_tab_della_lista_non_sono_il_nome_del_thread():
    """'Primary'/'General'/'Richieste' stanno a sinistra del bordo: se
    entrassero fra i candidati, ogni verifica post-click confronterebbe il nome
    della persona con un'etichetta dell'interfaccia."""
    nodi = [n for n in FASCIA_ALTA_MISURATA if n["testo"] in ("Primary", "General", "Richieste")]
    assert nome_header(nodi, bordo=471, larghezza_viewport=VIEWPORT_W) is None


def test_il_pulsante_in_fondo_a_destra_non_e_il_nome_del_thread():
    """'Top' a x=1197 e' nell'angolo opposto della colonna: fuori dalla fascia
    dove Instagram scrive il nome."""
    assert nome_header([{"left": 1197, "top": 80, "testo": "Top"}], bordo=471,
                       larghezza_viewport=VIEWPORT_W) is None


def test_header_non_ancora_renderizzato_non_produce_un_nome():
    assert nome_header([], bordo=471, larghezza_viewport=VIEWPORT_W) is None


# ── href del thread: lo username sta a 488 ─────────────────────────────────
def test_gli_href_del_thread_sono_quelli_a_destra_del_bordo():
    assert href_thread(HREF_MISURATI, bordo=471) == [
        "/emanuele_zerbi_/", "/emanuele_zerbi_", "/emanuele_zerbi_",
    ]


def test_gli_href_della_barra_laterale_restano_fuori():
    """/primero_adv3/ e' il link al proprio profilo nella barra sinistra: se
    entrasse, i candidati diventerebbero due e la riga verrebbe scartata."""
    assert "/primero_adv3/" not in href_thread(HREF_MISURATI, bordo=471)


# ── contenitore: la lista, non la conversazione ────────────────────────────
def test_a_chat_aperta_si_scrolla_la_lista_non_la_conversazione():
    """Il bug che dichiarava 'piantata' una lista da migliaia di chat: la
    conversazione ha scrollHeight PIU' GRANDE, quindi vinceva."""
    scelto = scegli_contenitore([PANNELLO_CONVERSAZIONE, LISTA_CHAT], bordo=471)
    assert scelto == 1


def test_senza_candidati_a_sinistra_del_bordo_non_si_scrolla_niente():
    """Meglio nessuno scroll che scrollare la conversazione: uno scroll sul
    pannello sbagliato non solleva errori, semplicemente non carica nulla."""
    assert scegli_contenitore([PANNELLO_CONVERSAZIONE], bordo=471) is None


def test_fra_due_contenitori_della_lista_vince_quello_piu_alto():
    interno = {"left": 72, "right": 471, "scrollHeight": 600, "clientHeight": 339}
    assert scegli_contenitore([interno, LISTA_CHAT], bordo=471) == 1


# ── scroll umano: non un salto secco ──────────────────────────────────────
def test_lo_scroll_e_fatto_di_piu_scatti_piccoli():
    """Un solo salto da 400px e' la firma piu' riconoscibile che ci sia:
    nessun dito su un trackpad muove una schermata in un evento solo."""
    piano = piano_scroll(400)
    assert len(piano) >= 3
    assert all(abs(delta) <= 200 for delta, _ in piano)


def test_lo_scroll_arriva_circa_dove_gli_e_stato_chiesto():
    """Circa, non esatto: la precisione al pixel e' essa stessa un segnale."""
    netto = sum(delta for delta, _ in piano_scroll(400))
    assert 340 <= netto <= 460


def test_fra_uno_scatto_e_l_altro_c_e_una_pausa_breve():
    assert all(0.01 <= pausa <= 0.35 for _, pausa in piano_scroll(400))


def test_gli_scatti_non_sono_tutti_uguali():
    """Passi identici sono una griglia: si riconoscono a colpo d'occhio in un
    grafico di eventi wheel."""
    delta = [d for d, _ in piano_scroll(400)]
    assert len(set(delta)) > 1


def test_ogni_tanto_lo_scroll_rimbalza_indietro():
    """Su cento gesti, almeno uno torna su di qualche pixel: e' quello che fa
    una mano vera quando supera il punto che voleva."""
    rimbalzi = sum(1 for _ in range(100)
                   if any(delta < 0 for delta, _ in piano_scroll(400)))
    assert rimbalzi > 0


def test_uno_scroll_di_zero_pixel_non_produce_gesti():
    assert piano_scroll(0) == []


# ── nessuna soglia assoluta puo' tornare dentro il JS ──────────────────────
def test_le_query_js_non_contengono_soglie_orizzontali_cablate():
    """L'unico test che vede il codice invece del comportamento, e c'e' un
    motivo: le funzioni pure qui sopra restano verdi anche se qualcuno rimette
    `left > 660` dentro una query JS, perche' il JS non gira in pytest. E'
    esattamente cosi' che il modulo e' arrivato in produzione capace di non
    salvare nemmeno un contatto, con la suite tutta verde.

    Le soglie VERTICALI (top) restano ammesse: separano l'intestazione dalla
    lista, non due colonne, e non dipendono dalla larghezza della finestra.
    """
    import re

    from app.services.inbox_browser import pagina

    sorgenti_js = {
        nome: valore for nome, valore in vars(pagina).items()
        if nome.startswith("_JS_") and isinstance(valore, str)
    }
    assert sorgenti_js, "nessuna query JS trovata: il test sta guardando nel posto sbagliato"

    cablate = {
        nome: re.findall(r"\br\.(?:left|right)\s*[<>]=?\s*\d+", sorgente)
        for nome, sorgente in sorgenti_js.items()
    }
    cablate = {nome: trovate for nome, trovate in cablate.items() if trovate}
    assert not cablate, (
        f"soglia orizzontale cablata nel JS: {cablate}. Il confine fra le colonne "
        "si misura con bordo_colonne e si decide in Python."
    )
