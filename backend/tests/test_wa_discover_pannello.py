"""Aprire una chat e leggere il pannello info senza sbagliare persona.

Le funzioni pure (titolo_combacia, numero_dal_pannello) si testano senza
browser. apri_e_leggi tocca `page`: si testa con una pagina finta, sullo
stesso principio di _FakePage in test_inbox_browser_pagina.py -- risponde in
modo diverso a seconda della query JS, cosi' risoluzione per contenuto,
verifica dell'header e lettura del pannello non si confondono a vicenda.
"""
import pytest

from app.services.wa_discover.pannello import (
    SEL_DRAWER_INFO, SEL_HEADER, apri_e_leggi, numero_dal_pannello, titolo_combacia,
)


# ── titolo_combacia: stessa regola di nome_combacia (inbox_browser.pagina) ──
def test_titolo_mancante_non_combacia_mai():
    """Meglio rinunciare a una riga che salvare un numero attribuito alla
    persona sbagliata. Stessa regola di inbox_browser.nome_combacia."""
    assert titolo_combacia(None, "Fulvio") is False
    assert titolo_combacia("Fulvio", None) is False
    assert titolo_combacia("", "") is False


def test_titolo_combacia_a_meno_di_spazi_e_maiuscole():
    assert titolo_combacia("  Fulvio CBD ", "fulvio cbd") is True


def test_titolo_diverso_non_combacia():
    assert titolo_combacia("Fulvio", "Fulvio CBD") is False


def test_titolo_con_emoji_combacia_lo_stesso():
    """normalizza_nome toglie gli emoji (categoria So/Sk/Cf): un titolo vero
    come 'PRIMERO 🤵👨‍🌾' deve combaciare con se stesso letto due volte, anche
    se WhatsApp lo restituisse con emoji leggermente diversi in punti diversi
    del DOM (title vs innerText)."""
    assert titolo_combacia("PRIMERO 🤵👨‍🌾", "primero") is True


# ── numero_dal_pannello: mai scambiare date/orari/conteggi per un numero ───
def test_legge_il_numero_dal_testo_del_pannello():
    testo = "Fulvio CBD\n+39 342 146 0077\nInfo contatto\nFile multimediali"
    assert numero_dal_pannello(testo) == "393421460077"


def test_pannello_di_gruppo_non_ha_un_numero():
    """Misurato PoC-4: 0% di numeri leggibili sui gruppi. Non e' un buco, e'
    corretto -- un gruppo non ha un numero singolo."""
    assert numero_dal_pannello("SPEDIZIONI\n12 partecipanti\nAggiungi partecipante") is None


def test_non_scambia_una_data_o_un_orario_per_un_numero():
    assert numero_dal_pannello("Fulvio\nUltimo accesso 11/08/2026 alle 14:35") is None


def test_pannello_vuoto_non_solleva():
    assert numero_dal_pannello("") is None
    assert numero_dal_pannello(None) is None


# ── apri_e_leggi: la trappola centrale (PoC-4, mismatch 4-5/8) ─────────────
class _FakeHandle:
    def __init__(self, elemento):
        self._elemento = elemento

    def as_element(self):
        return self._elemento


class _FakeElemento:
    """human_click chiama scroll_into_view_if_needed e bounding_box prima di
    muovere il mouse (human_input.py): il fake deve rispondere a entrambe."""

    async def scroll_into_view_if_needed(self, timeout=1500):
        return None

    async def bounding_box(self):
        return {"x": 100, "y": 200, "width": 300, "height": 40}


class _FakeTastiera:
    def __init__(self, outer):
        self._outer = outer

    async def press(self, tasto):
        if tasto == "Escape":
            self._outer.escape_premuti += 1


class _FakeMouse:
    async def move(self, x, y, steps=1):
        return None

    async def click(self, x, y):
        return None


class _FakePagePannello:
    """Simula la sidebar (titoli correnti), l'header del thread aperto e il
    pannello info. `titolo_header` e `testo_pannello` possono essere None per
    simulare l'header/pannello mai arrivato entro le attese (5% di
    fallimento misurato PoC-4 sul pannello)."""

    def __init__(self, titoli_sidebar, titolo_header, testo_pannello=None,
                 ritardo_header=0, ritardo_pannello=0):
        self.titoli_sidebar = titoli_sidebar
        self.titolo_header = titolo_header
        self.testo_pannello = testo_pannello
        self._letture_header = 0
        self._ritardo_header = ritardo_header
        self._letture_pannello = 0
        self._ritardo_pannello = ritardo_pannello
        self.mouse = _FakeMouse()
        self.keyboard = _FakeTastiera(self)
        self.escape_premuti = 0

    async def evaluate(self, script, *args):
        if "rows.map" in script:
            return self.titoli_sidebar
        if "nodo.innerText" in script:
            selettore = args[0]
            if selettore == SEL_HEADER:
                self._letture_header += 1
                if self._letture_header <= self._ritardo_header:
                    return None
                return self.titolo_header
            if selettore == SEL_DRAWER_INFO:
                self._letture_pannello += 1
                if self._letture_pannello <= self._ritardo_pannello:
                    return None
                return self.testo_pannello
        raise AssertionError(f"query JS non riconosciuta dal fake: {script[:60]!r}")

    async def evaluate_handle(self, script, args):
        if "rows[args.idx]" in script:
            return _FakeHandle(_FakeElemento())
        if "querySelector(selettore)" in script:
            return _FakeHandle(_FakeElemento())
        raise AssertionError(f"evaluate_handle non riconosciuto dal fake: {script[:60]!r}")

    async def wait_for_timeout(self, ms):
        return None


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """human_click fa un asyncio.sleep(0.05-0.15) reale (jitter anti-detect,
    human_input.py): nei test non serve aspettare davvero, e sommato sulle
    attese a pazienza crescente rallenterebbe la suite senza motivo."""
    from app.services.wa_discover import pannello

    async def click_immediato(page, elemento):
        return None

    monkeypatch.setattr(pannello.human_input, "human_click", click_immediato)


@pytest.mark.asyncio
async def test_apri_e_leggi_legge_numero_quando_tutto_combacia():
    page = _FakePagePannello(
        titoli_sidebar=["Mamma", "Fulvio CBD"],
        titolo_header="Fulvio CBD",
        testo_pannello="Fulvio CBD\n+39 342 146 0077\nInfo contatto",
    )
    esito = await apri_e_leggi(page, "Fulvio CBD")
    assert esito.numero == "393421460077"
    assert esito.testo_pannello == "Fulvio CBD\n+39 342 146 0077\nInfo contatto"


@pytest.mark.asyncio
async def test_apri_e_leggi_gruppo_ritorna_testo_senza_numero():
    """Un gruppo si apre e si legge correttamente: solo il numero manca,
    com'e' giusto che sia (0% di numeri leggibili sui gruppi, PoC-4)."""
    page = _FakePagePannello(
        titoli_sidebar=["SPEDIZIONI"],
        titolo_header="SPEDIZIONI",
        testo_pannello="SPEDIZIONI\n12 partecipanti\nAggiungi partecipante",
    )
    esito = await apri_e_leggi(page, "SPEDIZIONI")
    assert esito.numero is None
    assert "partecipanti" in esito.testo_pannello


@pytest.mark.asyncio
async def test_apri_e_leggi_riordino_lista_rinuncia_senza_salvare():
    """Prova del nove del PoC-4: la riga cliccata era quella giusta al momento
    del click, ma la lista si e' riordinata e si e' aperta un'altra chat.
    La verifica post-click deve accorgersene e NON ritornare nulla -- mai un
    numero attribuito alla persona sbagliata."""
    page = _FakePagePannello(
        titoli_sidebar=["Fulvio CBD"],
        titolo_header="Mamma",  # si e' aperta un'altra chat
        testo_pannello="Mamma\n+39 111 222 3333",
    )
    esito = await apri_e_leggi(page, "Fulvio CBD")
    assert esito.numero is None
    assert esito.testo_pannello == ""


@pytest.mark.asyncio
async def test_apri_e_leggi_riga_non_in_dom_non_clicca():
    """La riga non e' (piu') fra quelle attualmente nel DOM virtualizzato:
    nessun tentativo alla cieca, nessun click, si rinuncia e basta."""
    page = _FakePagePannello(titoli_sidebar=["Mamma"], titolo_header=None)
    esito = await apri_e_leggi(page, "Fulvio CBD")
    assert esito.numero is None
    assert esito.testo_pannello == ""


@pytest.mark.asyncio
async def test_apri_e_leggi_titolo_atteso_mancante_non_clicca():
    page = _FakePagePannello(titoli_sidebar=["Fulvio"], titolo_header="Fulvio")
    esito = await apri_e_leggi(page, None)
    assert esito.numero is None
    assert esito.testo_pannello == ""


@pytest.mark.asyncio
async def test_apri_e_leggi_header_lento_ma_entro_le_attese_riesce():
    """Stesso principio di apri_riga: pazienza crescente, non un'attesa fissa
    che scarterebbe come 'riordinata' una riga in realta' corretta."""
    page = _FakePagePannello(
        titoli_sidebar=["Fulvio CBD"],
        titolo_header="Fulvio CBD",
        testo_pannello="Fulvio CBD\n+39 342 146 0077",
        ritardo_header=2,
    )
    esito = await apri_e_leggi(page, "Fulvio CBD")
    assert esito.numero == "393421460077"


@pytest.mark.asyncio
async def test_apri_e_leggi_pannello_mai_arrivato_ritorna_titolo_verificato_senza_numero():
    """5% di fallimento misurato (PoC-4, 19/20): il pannello che non arriva
    NON deve invalidare l'apertura gia' verificata sull'header -- si perde
    solo il numero, non l'intera riga."""
    page = _FakePagePannello(
        titoli_sidebar=["Fulvio CBD"],
        titolo_header="Fulvio CBD",
        testo_pannello=None,
        ritardo_pannello=999,
    )
    esito = await apri_e_leggi(page, "Fulvio CBD")
    assert esito.numero is None
    assert esito.testo_pannello == ""


@pytest.mark.asyncio
async def test_apri_e_leggi_chiude_il_pannello_dopo_la_lettura():
    """Sola lettura: non si lascia il pannello aperto per la chat successiva
    del giro (stesso gesto di poc4_info_panel.py, Escape x2)."""
    page = _FakePagePannello(
        titoli_sidebar=["Fulvio CBD"],
        titolo_header="Fulvio CBD",
        testo_pannello="Fulvio CBD\n+39 342 146 0077",
    )
    await apri_e_leggi(page, "Fulvio CBD")
    assert page.escape_premuti >= 1


# --- La distinzione che l'orchestratore non poteva fare ---

@pytest.mark.asyncio
async def test_riordino_e_gruppo_non_sono_lo_stesso_esito():
    """I due casi che prima collassavano entrambi su (None, "").

    Sono opposti per chi orchestra il giro: una riga la cui verifica e' fallita
    NON va salvata (si e' aperta un'altra persona), una riga verificata senza
    numero VA salvata marcata -- e' il 100% dei gruppi, e la spec lo richiede
    (5.3/5.4). Con un unico None l'orchestratore doveva scegliere fra perdere
    tutti i gruppi e scrivere dati attribuiti alla persona sbagliata.
    """
    from app.services.wa_discover.pannello import (
        ESITO_NON_VERIFICATA, ESITO_VERIFICATA,
    )

    # Si apre la chat sbagliata: la lista si e' riordinata sotto il click.
    riordinata = _FakePagePannello(
        titoli_sidebar=["Fulvio CBD"], titolo_header="Mamma",
        testo_pannello="Mamma\n+39 333 111 2222",
    )
    esito_riordino = await apri_e_leggi(riordinata, "Fulvio CBD")
    assert esito_riordino.esito == ESITO_NON_VERIFICATA
    assert esito_riordino.salvabile is False

    # Persona giusta, ma e' un gruppo: nessun numero, e va salvata lo stesso.
    gruppo = _FakePagePannello(
        titoli_sidebar=["SPEDIZIONI"], titolo_header="SPEDIZIONI",
        testo_pannello="SPEDIZIONI\n12 partecipanti",
    )
    esito_gruppo = await apri_e_leggi(gruppo, "SPEDIZIONI")
    assert esito_gruppo.esito == ESITO_VERIFICATA
    assert esito_gruppo.numero is None
    assert esito_gruppo.salvabile is True, (
        "un gruppo verificato deve essere salvabile: senza, la Fase A perde "
        "tutti i gruppi in silenzio"
    )
