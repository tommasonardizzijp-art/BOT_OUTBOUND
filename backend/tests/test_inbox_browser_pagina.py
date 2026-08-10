"""Interazione con la pagina: qui si testano le parti pure e la logica di
decisione. Il browser reale e' coperto dal QA agent (Task 15), non da pytest.
"""
import pytest

from app.services.inbox_browser.pagina import (
    PASSO_SCROLL_MAX, RigaVisibile, StatoScorrimento, apri_riga, decidi_da_segnali,
    decidi_fine_lista, nome_combacia,
)


# ── verifica post-click ────────────────────────────────────────────────────
def test_nome_combacia_ignora_maiuscole_e_spazi():
    assert nome_combacia("Bruzzo  Abbigliamento", "bruzzo abbigliamento") is True


def test_nome_non_combacia_blocca():
    assert nome_combacia("Bruzzo Abbigliamento", "Max Fashion") is False


def test_nome_mancante_non_combacia_mai():
    """Meglio rinunciare a una riga che salvare dati attribuiti alla persona sbagliata."""
    assert nome_combacia(None, "Bruzzo") is False
    assert nome_combacia("Bruzzo", None) is False


# ── passo di scorrimento ───────────────────────────────────────────────────
def test_il_passo_non_supera_una_schermata():
    """Sopra il buffer renderizzato le righe si perdono IN SILENZIO."""
    assert PASSO_SCROLL_MAX <= 0.8


# ── fine lista / lento / piantato ──────────────────────────────────────────
def test_altezza_cresciuta_significa_continua():
    assert decidi_da_segnali(altezza_prima=1152, altezza_dopo=1872,
                             al_fondo=False, falliti_inbox=0, attese_esaurite=False) == "continua"


def test_altezza_ferma_ma_attese_non_esaurite_significa_continua():
    """La lentezza normale non deve mai essere scambiata per la fine."""
    assert decidi_da_segnali(altezza_prima=5112, altezza_dopo=5112,
                             al_fondo=True, falliti_inbox=0, attese_esaurite=False) == "continua"


def test_attese_esaurite_in_fondo_senza_fallimenti_e_fine():
    assert decidi_da_segnali(altezza_prima=5112, altezza_dopo=5112,
                             al_fondo=True, falliti_inbox=0, attese_esaurite=True) == "fine"


def test_richieste_inbox_fallite_significano_piantato():
    """Non si dichiara completata una lista che potrebbe avere altro sotto."""
    assert decidi_da_segnali(altezza_prima=5112, altezza_dopo=5112,
                             al_fondo=True, falliti_inbox=3, attese_esaurite=True) == "piantato"


def test_ferma_ma_non_in_fondo_e_anomalia():
    assert decidi_da_segnali(altezza_prima=5112, altezza_dopo=5112,
                             al_fondo=False, falliti_inbox=0, attese_esaurite=True) == "piantato"


# ── segnale "chat non letta" ───────────────────────────────────────────────
# Misurato nel Task 0: font-weight 600 sul nome = non letta (mai un valore
# intermedio). Il pallino blu e' conferma ridondante, non un segnale a se'.
# La logica DOM (getComputedStyle nel browser reale) e' fuori dalla portata di
# un unit test puro: qui si verifica solo che la dataclass porti il campo.
def test_riga_visibile_espone_il_campo_non_letta():
    riga = RigaVisibile(indice=0, nome="Bruzzo Abbigliamento", ultimo_nostro=False,
                         non_letta=True, testo_grezzo="Bruzzo Abbigliamento\nCiao")
    assert riga.non_letta is True


def test_riga_visibile_letta_di_default_esplicito():
    riga = RigaVisibile(indice=1, nome="Max Fashion", ultimo_nostro=True,
                         non_letta=False, testo_grezzo="Max Fashion\nTu: ok")
    assert riga.non_letta is False


# ── prova del nove: verifica post-click ────────────────────────────────────
class _FakeHandle:
    def __init__(self, elemento):
        self._elemento = elemento

    def as_element(self):
        return self._elemento


class _FakeElemento:
    pass


class _FakePage:
    """Simula una lista che si riordina fra il calcolo del riquadro e il click:
    la riga cliccata ('Bruzzo Abbigliamento') non e' quella che si apre
    ('Max Fashion'), esattamente come descritto in human_input.py:99-107."""

    def __init__(self):
        self.header = ["Max Fashion"]

    async def evaluate_handle(self, script, indice):
        return _FakeHandle(_FakeElemento())

    async def evaluate(self, script, *args):
        return self.header

    async def wait_for_timeout(self, ms):
        return None


@pytest.mark.asyncio
async def test_senza_verifica_post_click_apri_riga_salverebbe_la_riga_sbagliata(monkeypatch):
    """Prova del nove, ramo negativo: se si toglie il confronto nome, la
    funzione ritorna comunque uno username — proprio quello della riga
    sbagliata, in silenzio."""
    from app.services.inbox_browser import pagina

    async def human_click_senza_verifica(page, elemento):
        return None

    monkeypatch.setattr(pagina.human_input, "human_click", human_click_senza_verifica)
    monkeypatch.setattr(pagina, "nome_combacia", lambda atteso, trovato: True)  # verifica disattivata
    monkeypatch.setattr(pagina, "estrai_username_thread", lambda href, propri: "max.fashion")

    page = _FakePage()
    risultato = await apri_riga(page, indice=0, nome_atteso="Bruzzo Abbigliamento", lingua="it")

    assert risultato == "max.fashion"  # riga sbagliata salvata: la prova che serve la verifica


@pytest.mark.asyncio
async def test_con_verifica_post_click_apri_riga_rinuncia_alla_riga(monkeypatch):
    """Prova del nove, ramo positivo: con la verifica attiva (comportamento
    reale, non patchato), il disallineamento fa rinunciare alla riga."""
    from app.services.inbox_browser import pagina

    async def human_click_senza_verifica(page, elemento):
        return None

    monkeypatch.setattr(pagina.human_input, "human_click", human_click_senza_verifica)

    page = _FakePage()
    risultato = await apri_riga(page, indice=0, nome_atteso="Bruzzo Abbigliamento", lingua="it")

    assert risultato is None


# ── QA Task 15: il pannello del thread può arrivare dopo il primo controllo ──
class _FakePageHeaderLento:
    """Simula il proxy reale misurato in QA: l'header e' vuoto ai primi
    controlli e compare solo al terzo (pattern osservato: vuoto a 500ms,
    popolato a 1000ms). Con un'attesa fissa la riga sarebbe scartata come
    'lista riordinata' anche se il nome combacia."""

    def __init__(self, header_dopo_n_letture: int, header_finale: list[str]):
        self._letture = 0
        self._soglia = header_dopo_n_letture
        self._header_finale = header_finale

    async def evaluate_handle(self, script, indice):
        return _FakeHandle(_FakeElemento())

    async def evaluate(self, script, *args):
        self._letture += 1
        return self._header_finale if self._letture >= self._soglia else []

    async def wait_for_timeout(self, ms):
        return None


@pytest.mark.asyncio
async def test_header_lento_ma_entro_le_attese_apri_riga_riesce(monkeypatch):
    """QA Task 15 — bug reale trovato con l'account primero_azienda_cbd: sul
    proxy reale il pannello del thread compare spesso dopo 1-2s, non entro
    l'unica attesa fissa che c'era prima (1.5s) — verifiche fallite in
    silenzio su righe in realta' corrette. Con la pazienza crescente (fino a
    3s, ma si esce appena l'header compare) l'apertura riesce comunque."""
    from app.services.inbox_browser import pagina

    async def human_click_ok(page, elemento):
        return None

    monkeypatch.setattr(pagina.human_input, "human_click", human_click_ok)
    monkeypatch.setattr(pagina, "estrai_username_thread", lambda href, propri: "tuscanyhemp")

    # header vuoto al 1° controllo, popolato dal 2° in poi (equivalente a
    # "vuoto a 500ms, presente a 1000ms" misurato dal vivo).
    page = _FakePageHeaderLento(header_dopo_n_letture=2, header_finale=["Tuscanyhemp"])
    risultato = await apri_riga(page, indice=0, nome_atteso="Tuscanyhemp", lingua="it")

    assert risultato == "tuscanyhemp"


@pytest.mark.asyncio
async def test_header_mai_arrivato_apri_riga_rinuncia_comunque(monkeypatch):
    """Ramo negativo: se l'header non arriva MAI entro le attese, si rinuncia
    ancora — la pazienza in piu' non trasforma la verifica in un'attesa
    infinita ne' toglie la protezione contro un vero disallineamento."""
    from app.services.inbox_browser import pagina

    async def human_click_ok(page, elemento):
        return None

    monkeypatch.setattr(pagina.human_input, "human_click", human_click_ok)

    page = _FakePageHeaderLento(header_dopo_n_letture=999, header_finale=["Qualcuno Altro"])
    risultato = await apri_riga(page, indice=0, nome_atteso="Tuscanyhemp", lingua="it")

    assert risultato is None


# ── decidi_fine_lista: falliti nella finestra, non cumulativi di sessione ──
class _FakePageAttese:
    async def wait_for_timeout(self, ms):
        return None


@pytest.mark.asyncio
async def test_decidi_fine_lista_ignora_falliti_precedenti_alla_finestra(monkeypatch):
    """Fix round 1, Critical: un fallimento avvenuto MOLTO PRIMA nella sessione
    (30-55 minuti di durata) non deve marcare 'piantato' una fine-lista reale
    successiva se non ci sono NUOVI fallimenti nella finestra di attesa di
    QUESTA chiamata. Senza baseline/delta, len(falliti_inbox) grezzo resta > 0
    per il resto della sessione e ogni fine-lista vera diventa un falso allarme."""
    from app.services.inbox_browser import pagina

    async def scorri_ferma(_page):
        return StatoScorrimento(altezza=5112, al_fondo=True)   # mai cresce: nessun nuovo caricamento

    monkeypatch.setattr(pagina, "scorri", scorri_ferma)

    # Fallimenti gia' presenti PRIMA di entrare in decidi_fine_lista (simula un
    # hiccup avvenuto molto prima nella sessione).
    falliti_inbox = ["errore-vecchio-1", "errore-vecchio-2", "errore-vecchio-3"]

    esito = await decidi_fine_lista(_FakePageAttese(), falliti_inbox)

    assert esito == "fine"  # nessun fallimento NUOVO nella finestra di attesa
