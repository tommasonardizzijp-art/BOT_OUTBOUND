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
def test_il_passo_e_grande_ma_la_lettura_campiona_durante_il_gesto():
    """Il vecchio vincolo — mai piu' di una schermata — proteggeva dalle righe
    perse quando si leggeva solo a fine gesto. Ora si campiona ogni
    PX_FRA_LETTURE pixel, quindi il passo puo' crescere; ma il campionamento
    deve restare piu' fitto del buffer renderizzato, se no il vincolo torna
    valido e nessuno se ne accorge."""
    from app.services.inbox_browser.pagina import PX_FRA_LETTURE

    altezza_riga, righe_nel_buffer = 72, 13
    assert PX_FRA_LETTURE < altezza_riga * righe_nel_buffer / 2
    assert PASSO_SCROLL_MAX > 1.0


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


# ── il DOM finto parla come quello vero ────────────────────────────────────
# Dal 10/08 le query JS restituiscono dati grezzi CON LE COORDINATE e la
# decisione sta in Python (vedi pagina.py, regola di disegno). I fake qui sotto
# usano la geometria misurata su @primero_adv3: lista 72..471, colonna del
# thread da 488, nome dell'header a 544, finestra 1280x660. Fake senza
# coordinate passerebbero anche con un motore che non sa piu' leggere il DOM
# vero — ed e' esattamente il buco da cui e' passato il bug originale.
_BORDO = 471
_VIEWPORT = {"w": 1280, "h": 660}


def _dom_righe(testi: list[str]) -> dict:
    return {
        "viewport": _VIEWPORT,
        "righe": [
            {"indice": i, "testo": t, "left": 72, "right": _BORDO,
             "top": 321 + i * 72, "w": 399, "nonLetta": False, "pallinoConferma": False}
            for i, t in enumerate(testi)
        ],
    }


def _dom_fascia_alta(testi: list[str]) -> list[dict]:
    """I nodi dell'header del thread, a destra del bordo e in alto."""
    return [{"left": 544, "top": 18 + i * 23, "testo": t} for i, t in enumerate(testi)]


def _dom_href(href: list[str]) -> list[dict]:
    return [{"left": 488, "top": 16 + i * 40, "href": h} for i, h in enumerate(href)]


def _e_query_righe(script: str) -> bool:
    return "nonLetta" in script


def _e_query_href(script: str) -> bool:
    return 'href^="/"' in script


class _FakePage:
    """Simula il click per coordinate che si disallinea DOPO la risoluzione per
    contenuto: la riga giusta viene trovata e cliccata ('Bruzzo Abbigliamento',
    presente nelle righe correnti), ma quello che si apre davvero e' un altro
    thread ('Max Fashion') — il caso descritto in human_input.py:99-107 (un DM
    arriva nella finestra fra mossa e pressione del mouse). Risponde in modo
    diverso a seconda della query JS, cosi' la risoluzione per contenuto e la
    verifica dell'header non si confondono a vicenda."""

    def __init__(self, righe_testi=None, header=None):
        self.righe_testi = righe_testi if righe_testi is not None else ["Bruzzo Abbigliamento\nCiao"]
        self.header = header if header is not None else ["Max Fashion"]

    async def evaluate_handle(self, script, indice):
        return _FakeHandle(_FakeElemento())

    async def evaluate(self, script, *args):
        if _e_query_righe(script):
            return _dom_righe(self.righe_testi)
        if _e_query_href(script):
            return []
        return _dom_fascia_alta(self.header)

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

    def __init__(self, header_dopo_n_letture: int, header_finale: list[str], riga_testo: str = "Tuscanyhemp"):
        self._letture = 0
        self._soglia = header_dopo_n_letture
        self._header_finale = header_finale
        self._riga_testo = riga_testo

    async def evaluate_handle(self, script, indice):
        return _FakeHandle(_FakeElemento())

    async def evaluate(self, script, *args):
        # La query di risoluzione per contenuto (e quella dell'href, dopo la
        # verifica) non vanno contate come "letture" dell'header lento.
        if _e_query_righe(script):
            return _dom_righe([self._riga_testo])
        if _e_query_href(script):
            return []
        self._letture += 1
        if self._letture < self._soglia:
            return []
        return _dom_fascia_alta(self._header_finale)

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


# ── C1: risoluzione per CONTENUTO, non per indice ──────────────────────────
class _FakePageIndiceRecorder:
    """Registra l'indice effettivamente richiesto per l'handle da cliccare,
    cosi' i test possono verificare che apri_riga risolva per contenuto e non
    riusi l'indice stale ricevuto in ingresso."""

    def __init__(self, righe_testi, header):
        self.righe_testi = righe_testi
        self.header = header
        self.idx_richiesto = None

    async def evaluate_handle(self, script, indice):
        self.idx_richiesto = indice
        return _FakeHandle(_FakeElemento())

    async def evaluate(self, script, *args):
        if _e_query_righe(script):
            return _dom_righe(self.righe_testi)
        if _e_query_href(script):
            return []
        return _dom_fascia_alta(self.header)

    async def wait_for_timeout(self, ms):
        return None


@pytest.mark.asyncio
async def test_apri_riga_risolve_per_contenuto_non_per_indice_stale(monkeypatch):
    """C1 Critical: fra la lettura del lotto e il click possono essere avvenuti
    scroll (human_click fa scroll_into_view_if_needed ad ogni apertura, vedi
    human_input.py:96-98) che spostano la riga attesa a un indice diverso da
    quello letto in origine. La vecchia logica per indice avrebbe cliccato
    l'elemento sbagliato (indice 0 = 'Altra Persona'); la riga va ri-risolta per
    contenuto e cliccata al suo indice VERO (2)."""
    from app.services.inbox_browser import pagina

    async def human_click_ok(page, elemento):
        return None
    monkeypatch.setattr(pagina.human_input, "human_click", human_click_ok)
    monkeypatch.setattr(pagina, "estrai_username_thread", lambda href, propri: "bruzzo_abbigliamento")

    # Indice hint (0) ricevuto dalla lettura originale del lotto: nel frattempo
    # lo scroll ha spostato la riga attesa all'indice 2 del DOM corrente.
    page = _FakePageIndiceRecorder(
        righe_testi=["Altra Persona\nCiao", "Un Altro Profilo\nOk", "Bruzzo Abbigliamento\nGrazie mille"],
        header=["Bruzzo Abbigliamento"],
    )
    risultato = await apri_riga(page, indice=0, nome_atteso="Bruzzo Abbigliamento", lingua="it")

    assert page.idx_richiesto == 2, "doveva risolvere l'indice per CONTENUTO (2), non usare l'hint stale (0)"
    assert risultato == "bruzzo_abbigliamento"


@pytest.mark.asyncio
async def test_apri_riga_riga_assente_dal_dom_non_clicca(monkeypatch):
    """C1 Critical, ramo negativo: se la riga attesa e' uscita dal buffer
    virtualizzato (scorsa oltre il buffer renderizzato, vedi modulo docstring
    punto 1), non si clicca NULLA — la vecchia logica per indice avrebbe invece
    cliccato qualunque elemento si trovasse all'indice 0, in silenzio."""
    from app.services.inbox_browser import pagina

    chiamato = {"click": False}

    async def human_click_non_dovrebbe_essere_chiamato(page, elemento):
        chiamato["click"] = True
    monkeypatch.setattr(pagina.human_input, "human_click", human_click_non_dovrebbe_essere_chiamato)

    page = _FakePageIndiceRecorder(
        righe_testi=["Altra Persona\nCiao", "Un Altro Profilo\nOk"],
        header=[],
    )
    risultato = await apri_riga(page, indice=0, nome_atteso="Bruzzo Abbigliamento", lingua="it")

    assert risultato is None
    assert chiamato["click"] is False
    assert page.idx_richiesto is None, "evaluate_handle non doveva essere chiamato affatto"


# ── I3: propri= collegato con account_username ─────────────────────────────
@pytest.mark.asyncio
async def test_apri_riga_esclude_il_proprio_username_dai_candidati_href(monkeypatch):
    """I3: senza account_username collegato, un link al proprio profilo nel
    pannello (es. avatar/header) produce 2 candidati e estrai_username_thread
    ritorna None per design — riga scartata anche se il click era corretto.
    Con account_username passato, il proprio profilo viene escluso e resta un
    solo candidato: l'interlocutore vero."""
    from app.services.inbox_browser import pagina

    async def human_click_ok(page, elemento):
        return None
    monkeypatch.setattr(pagina.human_input, "human_click", human_click_ok)

    class _FakePageConHref:
        def __init__(self):
            self.righe_testi = ["Bruzzo Abbigliamento\nCiao"]
            self.header = ["Bruzzo Abbigliamento"]
            self.href = ["/bruzzo_abbigliamento/", "/mio_account_loggato/"]

        async def evaluate_handle(self, script, indice):
            return _FakeHandle(_FakeElemento())

        async def evaluate(self, script, *args):
            if _e_query_righe(script):
                return _dom_righe(self.righe_testi)
            if _e_query_href(script):
                return _dom_href(self.href)
            return _dom_fascia_alta(self.header)

        async def wait_for_timeout(self, ms):
            return None

    page = _FakePageConHref()
    risultato = await apri_riga(
        page, indice=0, nome_atteso="Bruzzo Abbigliamento", lingua="it",
        account_username="mio_account_loggato",
    )

    assert risultato == "bruzzo_abbigliamento"


@pytest.mark.asyncio
async def test_apri_riga_senza_account_username_due_candidati_scarta_la_riga(monkeypatch):
    """Ramo negativo (prova del nove per I3): stesso scenario ma SENZA collegare
    account_username — il comportamento pre-fix. Due candidati href, nessuno
    escluso: estrai_username_thread ritorna None per design (thread di gruppo o
    ambiguo, vedi testo.py), la riga corretta viene scartata."""
    from app.services.inbox_browser import pagina

    async def human_click_ok(page, elemento):
        return None
    monkeypatch.setattr(pagina.human_input, "human_click", human_click_ok)

    class _FakePageConHref:
        def __init__(self):
            self.righe_testi = ["Bruzzo Abbigliamento\nCiao"]
            self.header = ["Bruzzo Abbigliamento"]
            self.href = ["/bruzzo_abbigliamento/", "/mio_account_loggato/"]

        async def evaluate_handle(self, script, indice):
            return _FakeHandle(_FakeElemento())

        async def evaluate(self, script, *args):
            if _e_query_righe(script):
                return _dom_righe(self.righe_testi)
            if _e_query_href(script):
                return _dom_href(self.href)
            return _dom_fascia_alta(self.header)

        async def wait_for_timeout(self, ms):
            return None

    page = _FakePageConHref()
    risultato = await apri_riga(page, indice=0, nome_atteso="Bruzzo Abbigliamento", lingua="it")

    assert risultato is None


# ── transizione fra due thread: l'header vecchio e' ancora li' ─────────────
class _FakePageTransizione:
    """Il pannello del thread non si svuota nell'istante del click: per qualche
    decimo di secondo l'header mostra ancora la chat PRECEDENTE. Misurato dal
    vivo il 10/08 aprendo tre chat di fila: le verifiche hanno letto 'None',
    'Verificato' e perfino lo username di un'altra conversazione — righe buone
    scartate in silenzio. L'URL /direct/t/<id>/ e' il segnale che la
    transizione e' finita: cambia solo quando il thread nuovo e' quello aperto.
    """

    def __init__(self):
        self.url = "https://www.instagram.com/direct/t/111/"
        self._letture_header = 0

    async def evaluate_handle(self, script, indice):
        return _FakeHandle(_FakeElemento())

    async def evaluate(self, script, *args):
        if _e_query_righe(script):
            return _dom_righe(["Nuova Persona\nCiao"])
        if _e_query_href(script):
            return _dom_href(["/nuova_persona/"] if self._transizione_finita else ["/vecchia_persona/"])
        self._letture_header += 1
        if self._transizione_finita:
            return _dom_fascia_alta(["Nuova Persona"])
        return _dom_fascia_alta(["Vecchia Persona"])

    @property
    def _transizione_finita(self):
        """Dalla seconda lettura in poi il thread nuovo e' montato: l'URL cambia
        insieme all'header."""
        if self._letture_header >= 2:
            self.url = "https://www.instagram.com/direct/t/222/"
            return True
        return False

    async def wait_for_timeout(self, ms):
        return None


@pytest.mark.asyncio
async def test_apri_riga_aspetta_che_il_thread_sia_cambiato_prima_di_verificare(monkeypatch):
    """Senza attendere il cambio di thread, la verifica confronta il nome della
    riga cliccata con l'header della chat PRECEDENTE: mismatch garantito, riga
    buona buttata, e nessun errore da nessuna parte."""
    from app.services.inbox_browser import pagina

    async def human_click_ok(page, elemento):
        return None

    monkeypatch.setattr(pagina.human_input, "human_click", human_click_ok)

    page = _FakePageTransizione()
    risultato = await apri_riga(page, indice=0, nome_atteso="Nuova Persona", lingua="it",
                                account_username="primero_adv3")

    assert risultato == "nuova_persona"


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

    async def scorri_leggendo_fermo(_page, _lingua, _su_righe):
        return StatoScorrimento(altezza=5112, al_fondo=True)   # mai cresce: nessun nuovo caricamento

    monkeypatch.setattr(pagina, "scorri_leggendo", scorri_leggendo_fermo)

    # Fallimenti gia' presenti PRIMA di entrare in decidi_fine_lista (simula un
    # hiccup avvenuto molto prima nella sessione).
    falliti_inbox = ["errore-vecchio-1", "errore-vecchio-2", "errore-vecchio-3"]

    async def _ignora(_righe):
        return None

    esito = await decidi_fine_lista(_FakePageAttese(), falliti_inbox, "it", _ignora)

    assert esito == "fine"  # nessun fallimento NUOVO nella finestra di attesa


# ── leggere durante il gesto ───────────────────────────────────────────────
class _FakePageScroll:
    """Finge una lista virtualizzata: tiene nel DOM solo le righe vicine alla
    posizione corrente, come fa Instagram. Serve a dimostrare che leggendo solo
    a gesto finito le righe intermedie non si vedono MAI."""

    ALTEZZA_RIGA = 72
    RIGHE_NEL_DOM = 13

    def __init__(self, quante=200):
        self.tutte = [f"Contatto {i}" for i in range(quante)]
        self.scroll = 0
        self.eventi = 0

    @property
    def url(self):
        return "https://www.instagram.com/direct/inbox/"

    async def evaluate(self, script, *args):
        if "nonLetta" in script:
            primo = self.scroll // self.ALTEZZA_RIGA
            finestra = self.tutte[primo:primo + self.RIGHE_NEL_DOM]
            return {
                "viewport": {"w": 1920, "h": 940},
                "righe": [
                    {"indice": i, "testo": t, "left": 72, "right": 471,
                     "top": 200 + i * self.ALTEZZA_RIGA, "w": 399,
                     "nonLetta": False, "pallinoConferma": False}
                    for i, t in enumerate(finestra)
                ],
            }
        if "overflowY" in script and "idx" not in script:
            return [{"left": 72, "right": 471, "top": 200, "w": 399, "h": 700,
                     "scrollHeight": len(self.tutte) * self.ALTEZZA_RIGA,
                     "clientHeight": 700, "scrollTop": self.scroll}]
        if "idx" in script:
            return {"altezza": len(self.tutte) * self.ALTEZZA_RIGA,
                    "top": self.scroll, "visibile": 700, "alFondo": False}
        return []

    async def wait_for_timeout(self, ms):
        return None

    class _Mouse:
        def __init__(self, pagina):
            self.pagina = pagina

        async def move(self, x, y, steps=1):
            return None

        async def wheel(self, dx, dy):
            self.pagina.scroll += int(dy)
            self.pagina.eventi += 1

    @property
    def mouse(self):
        return _FakePageScroll._Mouse(self)


@pytest.mark.asyncio
async def test_leggendo_solo_a_fine_gesto_le_righe_di_mezzo_spariscono():
    """La prova del nove: senza campionamento, un gesto lungo attraversa righe
    che nessuno vede mai. E' esattamente la perdita silenziosa che il limite di
    0.7 schermate serviva a evitare.

    Il secondo checkpoint resta sotto i ~2.700px (45 scatti x 60px) a cui il
    gesto provvisorio pre-Task-3 e' ancora limitato: usare "Contatto 40" come
    nel piano originale sarebbe borderline con questi parametri e renderebbe
    il test dipendente dalla riuscita di Task 3, non ancora fatto a questo
    punto del percorso critico (2 -> 4a -> 3)."""
    from app.services.inbox_browser.pagina import piano_scroll

    page = _FakePageScroll()
    for delta, _ in piano_scroll(6000):
        await page.mouse.wheel(0, delta)
    righe = (await page.evaluate("nonLetta"))["righe"]
    nomi_visti = {r["testo"] for r in righe}
    assert "Contatto 5" not in nomi_visti     # attraversata e mai vista
    assert "Contatto 15" not in nomi_visti


@pytest.mark.asyncio
async def test_scorrendo_e_leggendo_nessuna_riga_viene_persa():
    from app.services.inbox_browser.pagina import scorri_leggendo

    page = _FakePageScroll()
    viste: set[str] = set()

    async def raccogli(righe):
        for r in righe:
            viste.add(r.nome)

    await scorri_leggendo(page, "it", raccogli)

    attraversate = page.scroll // _FakePageScroll.ALTEZZA_RIGA
    mancanti = [f"Contatto {i}" for i in range(attraversate) if f"Contatto {i}" not in viste]
    assert not mancanti, f"{len(mancanti)} righe attraversate e mai viste: {mancanti[:6]}"


@pytest.mark.asyncio
async def test_il_campionamento_non_rallenta_il_gesto():
    """Leggere costa 0.06% del tempo di sessione (misurato). Il numero di
    letture deve restare proporzionale alla distanza, non agli eventi: una
    lettura per evento wheel sarebbe 200 letture per gesto."""
    from app.services.inbox_browser.pagina import PX_FRA_LETTURE, scorri_leggendo

    page = _FakePageScroll()
    letture = 0

    async def conta(righe):
        nonlocal letture
        letture += 1

    await scorri_leggendo(page, "it", conta)
    assert letture <= (page.scroll // PX_FRA_LETTURE) + 2
