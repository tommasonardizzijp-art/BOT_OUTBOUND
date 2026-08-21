"""Test del POM WhatsApp Web (M1, Task 4). Nessun browser: tutto contro
oggetti finti o funzioni pure. L'E2E sul browser vero e' fuori scope di
questo task (fermato allo Step 7 del brief)."""
import asyncio

import pytest

from app.browser import whatsapp_selectors as sel
from app.browser.whatsapp_page import classify_direction


@pytest.mark.parametrize("segnali,atteso", [
    # (aria_tu, tail_icon, data_id) -> "out" | "in"
    ((True,  "tail-out", "A5" + "x" * 30), "out"),   # tutti concordi: nostro
    ((False, "tail-in",  "3A" + "x" * 18), "in"),    # tutti concordi: loro
    ((False, None,       "A5" + "x" * 30), "out"),   # solo il data_id: basta
    ((False, None,       None),            "in"),    # nessun segnale -> inbound
    ((True,  "tail-in",  "A5" + "x" * 30), "in"),    # DISCORDANTI -> inbound
    ((False, "tail-in",  "A5" + "x" * 30), "in"),    # DISCORDANTI -> inbound
])
def test_direzione_in_dubbio_vale_inbound(segnali, atteso):
    """Asimmetria deliberata: un messaggio e' 'nostro' solo se ALMENO UN segnale
    dice OUT e NESSUNO dice IN.

    I due errori non costano uguale. Trattare un nostro messaggio come inbound
    = si legge qualcosa in piu' e al peggio non si invia. Trattare un loro
    messaggio come nostro = la guardia salta lo STOP e si scrive a chi aveva
    chiesto di smettere. Il secondo e' irreversibile.
    """
    aria_tu, tail_icon, data_id = segnali
    assert classify_direction(aria_tu=aria_tu, tail_icon=tail_icon, data_id=data_id) == atteso


@pytest.mark.asyncio
async def test_tail_none_quando_il_dom_non_aggancia_nulla(monkeypatch):
    """None (cecita') non e' [] (silenzio). Se un selettore si rompe e il POM
    tornasse [], il chiamante concluderebbe 'nessuno STOP' e invierebbe SEMPRE,
    sembrando funzionare. E' esattamente il bug che M0 ha evitato con la
    sentinella."""
    from app.browser.whatsapp_page import WhatsAppWebPage

    class PageSenzaBolle:
        async def evaluate(self, _script, *_a):
            return None

    pom = WhatsAppWebPage(PageSenzaBolle())
    assert await pom.read_inbound_tail() is None


@pytest.mark.asyncio
async def test_tail_vuota_e_diversa_da_tail_assente():
    """Silenzio VERO: bolle presenti (righe non vuote), tutte OUT -> tail ==
    []. Diverso dal caso sotto (adversarial 13): li' evaluate() torna []
    ALLA PRIMA battuta, prima di qualunque classificazione -- e' un
    contratto rotto (il JS promette null per zero righe), non silenzio."""
    from app.browser.whatsapp_page import WhatsAppWebPage

    riga_out = {
        "aria_tu": True, "tail_icon": "tail-out",
        "data_id": sel.DATA_ID_OUT_PREFIX + "x" * (sel.DATA_ID_OUT_LEN - len(sel.DATA_ID_OUT_PREFIX)),
        "text": "ciao, tutto ok",
    }

    class PageSoloOutbound:
        async def evaluate(self, _script, *_a):
            return [riga_out, riga_out]

    assert await WhatsAppWebPage(PageSoloOutbound()).read_inbound_tail() == []


@pytest.mark.asyncio
async def test_evaluate_lista_vuota_e_violazione_di_contratto_non_silenzio():
    """Adversarial 13. _JS_MESSAGE_SIGNALS promette null per zero righe
    agganciate (vedi il suo commento in whatsapp_page.py): una lista vuota
    alla radice e' quindi cecita', non silenzio. Prima della correzione
    (2026-07-28) read_inbound_tail tornava [] anche qui, indistinguibile dal
    silenzio vero -- il chiamante avrebbe letto 'nessuno STOP' e inviato
    sempre."""
    from app.browser.whatsapp_page import WhatsAppWebPage

    class PageContrattoRotto:
        async def evaluate(self, _script, *_a):
            return []

    assert await WhatsAppWebPage(PageContrattoRotto()).read_inbound_tail() is None


@pytest.mark.asyncio
async def test_sync_state_unknown_finche_il_selettore_non_e_catalogato():
    """A9: WhatsApp Web non sincronizza tutte le chat subito. Su una chat non
    ancora sincronizzata la guardia non legge un silenzio, legge il VUOTO.

    Il selettore dell'indicatore non e' ancora catalogato: catturarlo richiede
    un re-scan del QR, che azzererebbe PoC-1. Quindi in M1 sync_state() esiste
    con la sua interfaccia e torna 'unknown', ed e' la POLITICA (M3) a decidere
    cosa fare di 'unknown'. Quello che NON si fa e' far finta che 'unknown'
    sia 'synced'."""
    from app.browser.whatsapp_page import WhatsAppWebPage

    class PageQualunque:
        async def evaluate(self, _s, *_a):
            return None

        def locator(self, _sel):
            raise AssertionError("nessun selettore catalogato: non si inventa")

    assert await WhatsAppWebPage(PageQualunque()).sync_state() == "unknown"


# ---------------------------------------------------------------------------
# Adversarial C (14-19) -- read_inbound_tail, DOM ostile oltre a None/[].
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_evaluate_stringa_e_cecita():
    """Adversarial 14. evaluate() torna una stringa invece di una lista/null:
    non e' il contratto promesso, quindi cecita'."""
    from app.browser.whatsapp_page import WhatsAppWebPage

    class PageStringa:
        async def evaluate(self, _script, *_a):
            return "ok"

    assert await WhatsAppWebPage(PageStringa()).read_inbound_tail() is None


@pytest.mark.asyncio
async def test_evaluate_dict_e_cecita():
    """Adversarial 15."""
    from app.browser.whatsapp_page import WhatsAppWebPage

    class PageDict:
        async def evaluate(self, _script, *_a):
            return {"a": 1}

    assert await WhatsAppWebPage(PageDict()).read_inbound_tail() is None


@pytest.mark.asyncio
async def test_evaluate_lista_mista_e_cecita_non_coda_parziale():
    """Adversarial 16. Un dict valido + una stringa: NON deve tornare la coda
    dei soli elementi validi (coda parziale = falso senso di sicurezza)."""
    from app.browser.whatsapp_page import WhatsAppWebPage

    riga_valida = {"aria_tu": False, "tail_icon": None, "data_id": None, "text": "ciao"}

    class PageMista:
        async def evaluate(self, _script, *_a):
            return [riga_valida, "stringa-non-un-dict"]

    assert await WhatsAppWebPage(PageMista()).read_inbound_tail() is None


@pytest.mark.asyncio
async def test_evaluate_righe_senza_chiavi_attese_e_cecita():
    """Adversarial 17 (regressione d8e744c): senza le chiavi giuste, un
    default silenzioso (.get(k, '')) produceva stringhe vuote invisibili a
    un controllo di STOP. Qui manca data_id."""
    from app.browser.whatsapp_page import WhatsAppWebPage

    riga_incompleta = {"aria_tu": False, "tail_icon": None, "text": "ciao"}

    class PageIncompleta:
        async def evaluate(self, _script, *_a):
            return [riga_incompleta]

    assert await WhatsAppWebPage(PageIncompleta()).read_inbound_tail() is None


@pytest.mark.asyncio
async def test_evaluate_text_none_non_finisce_in_coda():
    """Adversarial 18. Le 4 chiavi ci sono ma text e' None: il JS promette
    SEMPRE una stringa per text (slice di innerText, mai null), quindi un
    None qui e' malformato quanto una chiave mancante. Prima della
    correzione (2026-07-28) _righe_ben_formate controllava solo le chiavi,
    non i tipi: un None sarebbe finito dentro la coda e un chiamante che fa
    'STOP' in t.upper() sarebbe esploso con AttributeError."""
    from app.browser.whatsapp_page import WhatsAppWebPage

    riga_rotta = {"aria_tu": False, "tail_icon": None, "data_id": None, "text": None}

    class PageTextNone:
        async def evaluate(self, _script, *_a):
            return [riga_rotta]

    risultato = await WhatsAppWebPage(PageTextNone()).read_inbound_tail()
    assert risultato is None


@pytest.mark.asyncio
async def test_evaluate_che_solleva_propaga():
    """Adversarial 19. Un errore rumoroso e' meglio di una cecita' silenziosa:
    l'eccezione non va catturata producendo una coda vuota."""
    from app.browser.whatsapp_page import WhatsAppWebPage

    class PageRotta:
        async def evaluate(self, _script, *_a):
            raise RuntimeError("DOM disconnesso")

    with pytest.raises(RuntimeError):
        await WhatsAppWebPage(PageRotta()).read_inbound_tail()


# ---------------------------------------------------------------------------
# Funzionale D-20 -- read_inbound_tail non si ferma al primo messaggio nostro.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_inbound_tail_coda_mista_torna_tutti_gli_inbound_in_ordine():
    """Coda IN/OUT/IN/OUT/IN (ordine DOM = cronologico): tutti e tre gli
    inbound tornano, in ordine cronologico, non solo il piu' recente."""
    from app.browser.whatsapp_page import WhatsAppWebPage

    def riga_in(testo):
        return {
            "aria_tu": False, "tail_icon": None,
            "data_id": sel.DATA_ID_IN_PREFIX + "x" * (sel.DATA_ID_IN_LEN - len(sel.DATA_ID_IN_PREFIX)),
            "text": testo,
        }

    def riga_out(testo):
        return {
            "aria_tu": True, "tail_icon": "tail-out",
            "data_id": sel.DATA_ID_OUT_PREFIX + "x" * (sel.DATA_ID_OUT_LEN - len(sel.DATA_ID_OUT_PREFIX)),
            "text": testo,
        }

    righe = [riga_in("in1"), riga_out("out1"), riga_in("in2"), riga_out("out2"), riga_in("in3")]

    class PageMista:
        async def evaluate(self, _script, *_a):
            return righe

    tail = await WhatsAppWebPage(PageMista()).read_inbound_tail()
    assert tail == ["in1", "in2", "in3"]


# ---------------------------------------------------------------------------
# parse_wa_timestamp + read_inbound_since -- fix 21/08 (backfill chat_title
# corrotto): read_inbound_tail non ha nozione di tempo, e riusarlo per
# decidere 'ha risposto' produceva falsi positivi su corrispondenza
# organica precedente e sull'avviso di crittografia (nessun mittente).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_parse_wa_timestamp_formato_verificato_dal_vivo():
    """Formato VERIFICATO dal vivo il 21/08 (diag_wa_timestamp.py, chat
    reali): '[HH:MM, DD/MM/YYYY] Mittente: '."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.browser.whatsapp_page import parse_wa_timestamp

    ts = parse_wa_timestamp("[14:32, 19/08/2026] Mario Rossi: ")
    assert ts == datetime(2026, 8, 19, 14, 32, tzinfo=ZoneInfo("Europe/Rome"))


@pytest.mark.parametrize("pre_plain_text", [None, "", "testo senza data", "[14:32] senza data"])
def test_parse_wa_timestamp_none_su_prefisso_assente_o_malformato(pre_plain_text):
    """L'avviso di crittografia end-to-end (nessun mittente) non ha
    data-pre-plain-text affatto: None, non una data indovinata."""
    from app.browser.whatsapp_page import parse_wa_timestamp
    assert parse_wa_timestamp(pre_plain_text) is None


@pytest.mark.asyncio
async def test_read_inbound_since_scarta_risposta_prima_del_nostro_invio():
    """Bug reale 21/08: un messaggio inbound VECCHIO (corrispondenza
    organica precedente sullo stesso numero, prima ancora della campagna)
    non deve contare come risposta alla campagna."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.browser.whatsapp_page import WhatsAppWebPage

    riga_vecchia = {
        "aria_tu": False, "tail_icon": None, "data_id": None,
        "text": "messaggio di un anno fa",
        "pre_plain_text": "[16:04, 12/08/2025] +39 345 424 9895: ",
    }

    class PageStoricoVecchio:
        async def evaluate(self, _script, *_a):
            return [riga_vecchia]

    dopo = datetime(2026, 8, 19, 9, 49, tzinfo=ZoneInfo("Europe/Rome"))
    testi = await WhatsAppWebPage(PageStoricoVecchio()).read_inbound_since(dopo)
    assert testi == []


@pytest.mark.asyncio
async def test_read_inbound_since_scarta_riga_di_sistema_senza_mittente():
    """L'avviso 'i messaggi sono crittografati end-to-end' non ha
    data-pre-plain-text (nessun mittente): classify_direction lo conta 'in'
    per sicurezza sullo STOP (read_inbound_tail, invariato), ma qui
    produceva un falso 'ha risposto' -- misurato dal vivo, 3 falsi positivi
    su 4 nel pilota del backfill."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.browser.whatsapp_page import WhatsAppWebPage

    riga_sistema = {
        "aria_tu": False, "tail_icon": None, "data_id": None,
        "text": "I messaggi e le chiamate sono crittografati end-to-end.",
        "pre_plain_text": None,
    }

    class PageSoloSistema:
        async def evaluate(self, _script, *_a):
            return [riga_sistema]

    dopo = datetime(2020, 1, 1, tzinfo=ZoneInfo("Europe/Rome"))
    testi = await WhatsAppWebPage(PageSoloSistema()).read_inbound_since(dopo)
    assert testi == []


@pytest.mark.asyncio
async def test_read_inbound_since_accetta_risposta_genuina_entro_la_finestra():
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from app.browser.whatsapp_page import WhatsAppWebPage

    riga_vera = {
        "aria_tu": False, "tail_icon": None, "data_id": None,
        "text": "Scorta",
        "pre_plain_text": "[20:40, 18/08/2026] +39 345 424 9895: ",
    }

    class PageRispostaVera:
        async def evaluate(self, _script, *_a):
            return [riga_vera]

    dopo = datetime(2026, 8, 18, 17, 16, tzinfo=ZoneInfo("Europe/Rome"))
    entro = dopo + timedelta(hours=48)
    testi = await WhatsAppWebPage(PageRispostaVera()).read_inbound_since(dopo, entro=entro)
    assert testi == ["Scorta"]


@pytest.mark.asyncio
async def test_read_inbound_since_scarta_risposta_oltre_la_finestra_entro():
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from app.browser.whatsapp_page import WhatsAppWebPage

    riga_tardiva = {
        "aria_tu": False, "tail_icon": None, "data_id": None,
        "text": "rispondo tardi",
        "pre_plain_text": "[10:00, 25/08/2026] +39 345 424 9895: ",
    }

    class PageTardiva:
        async def evaluate(self, _script, *_a):
            return [riga_tardiva]

    dopo = datetime(2026, 8, 18, 17, 16, tzinfo=ZoneInfo("Europe/Rome"))
    entro = dopo + timedelta(hours=48)
    testi = await WhatsAppWebPage(PageTardiva()).read_inbound_since(dopo, entro=entro)
    assert testi == []


@pytest.mark.asyncio
async def test_read_inbound_since_none_su_cecita():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.browser.whatsapp_page import WhatsAppWebPage

    class PageCieca:
        async def evaluate(self, _script, *_a):
            return None

    dopo = datetime(2026, 8, 18, tzinfo=ZoneInfo("Europe/Rome"))
    assert await WhatsAppWebPage(PageCieca()).read_inbound_since(dopo) is None


# ---------------------------------------------------------------------------
# Adversarial D (20-21) + Funzionale D-21 + Adversarial D (22-23) --
# scan_chat_list, DOM ostile.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_chat_list_pane_non_trovato_alza_runtimeerror():
    """Adversarial 20. Mai una lista vuota (si leggerebbe come 'nessuna
    chat'): un RuntimeError che nomina i selettori del catalogo."""
    from app.browser.whatsapp_page import WhatsAppWebPage

    class PagePaneAssente:
        async def evaluate(self, _script, *_a):
            return {"error": "pane non trovato"}

    with pytest.raises(RuntimeError, match="selettori del catalogo"):
        await WhatsAppWebPage(PagePaneAssente()).scan_chat_list()


@pytest.mark.asyncio
async def test_scan_chat_list_riga_senza_title_diagnostica_non_keyerror():
    """Adversarial 21. Prima della correzione (2026-07-28) r['title'] dava un
    KeyError grezzo dentro il ciclo -- niente indicazione di dove guardare.
    Ora un RuntimeError che nomina la chiave mancante e i selettori del
    catalogo."""
    from app.browser.whatsapp_page import WhatsAppWebPage

    riga_senza_title = {
        "position": 0, "unread_raw": "", "preview": "",
        "last_is_outbound": False, "outgoing_state": None, "muted": False,
    }

    class PageRigaRotta:
        async def evaluate(self, _script, *_a):
            return [riga_senza_title]

    with pytest.raises(RuntimeError, match="title"):
        await WhatsAppWebPage(PageRigaRotta()).scan_chat_list()


@pytest.mark.asyncio
async def test_scan_chat_list_non_clicca_e_legge_i_segnali_grezzi():
    """Funzionale 21 + adversarial 22/23 insieme: scan_chat_list chiama SOLO
    evaluate (la pagina finta qui sotto non implementa .locator/.click: se il
    metodo li chiamasse il test fallirebbe con AttributeError, prova che
    'nessun click sulla pagina finta' e' rispettato).

    Riga 1: titolo-numero CON marcatori bidi ANCORA presenti (seconda rete di
    _BIDI_MARKERS oltre al pulisci() del JS, adversarial 22) -> title_is_number
    resta True. Riga 2: badge senza cifre -> unread_count 1 (adversarial 23)."""
    from app.browser.whatsapp_page import WhatsAppWebPage

    righe = [
        {
            "position": 0, "title": "Mario Rossi",
            "unread_raw": "3 messaggi non letti", "preview": "ciao",
            "last_is_outbound": False, "outgoing_state": None, "muted": False,
            "is_yourself": False,
        },
        {
            "position": 1, "title": chr(0x202a) + "393421460077" + chr(0x202c),
            "unread_raw": "", "preview": "",
            "last_is_outbound": True, "outgoing_state": "wds-ic-read", "muted": False,
            "is_yourself": False,
        },
        {
            "position": 2, "title": "Solo Icona",
            "unread_raw": chr(0x1F514), "preview": "",
            "last_is_outbound": False, "outgoing_state": None, "muted": True,
            "is_yourself": False,
        },
        {
            "position": 3, "title": "Tommaso Nardizzi",
            "unread_raw": "", "preview": "",
            "last_is_outbound": True, "outgoing_state": "wds-ic-sent", "muted": False,
            "is_yourself": True,
        },
    ]

    class PageScan:
        async def evaluate(self, _script, _args):
            return righe

    rows = await WhatsAppWebPage(PageScan()).scan_chat_list()

    assert rows[0].title == "Mario Rossi"
    assert rows[0].title_is_number is False
    assert rows[0].unread_count == 3

    assert rows[1].title_is_number is True   # i marcatori bidi non lo fanno sembrare un nome
    assert rows[1].unread_count == 0         # '' -> 0

    assert rows[2].unread_count == 1         # testo senza cifre -> 1
    assert rows[2].muted is True

    assert rows[3].is_yourself is True
    assert rows[0].is_yourself is False      # una riga normale non e' "te stesso"


@pytest.mark.parametrize("raw,atteso", [
    ("3 messaggi non letti", 3),
    ("1.234", 1234),
    ("", 0),
    (chr(0x1F514), 1),   # icona senza cifre
    (None, 0),           # ostile: nessuna eccezione (era un TypeError)
])
def test_parse_unread_ostile(raw, atteso):
    """Adversarial 23."""
    from app.browser.whatsapp_page import _parse_unread

    assert _parse_unread(raw) == atteso


# ---------------------------------------------------------------------------
# Funzionale D (14-16) -- session_state, con le costanti del catalogo.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_state_usa_le_costanti_non_literal(monkeypatch):
    """Funzionale 14. Sentinella: monkeypatcha i valori delle costanti a
    numeri distintivi mai usati altrove. Se session_state avesse un literal
    nudo (la regressione a 8000 gia' rientrata una volta), il monkeypatch non
    lo toccherebbe e la chiamata registrata non coinciderebbe col sentinel."""
    from app.browser import whatsapp_page

    monkeypatch.setattr(sel, "SESSION_STATE_TIMEOUT_CHATLIST_MS", 911001)
    monkeypatch.setattr(sel, "SESSION_STATE_TIMEOUT_QR_MS", 911002)

    chiamate: list[int] = []

    async def _fake_first_locator(self, candidates, timeout_ms=4000):
        chiamate.append(timeout_ms)
        return None

    monkeypatch.setattr(whatsapp_page.WhatsAppWebPage, "_first_locator", _fake_first_locator)

    pom = whatsapp_page.WhatsAppWebPage(page=None)
    assert await pom.session_state() == "unknown"
    assert chiamate == [911001, 911002]


@pytest.mark.asyncio
async def test_session_state_logged_in_quando_aggancia_la_chatlist(monkeypatch):
    """Funzionale 14."""
    from app.browser.whatsapp_page import WhatsAppWebPage

    async def _fake_first_locator(self, candidates, timeout_ms=4000):
        return (object(), "chatlist-selettore")

    monkeypatch.setattr(WhatsAppWebPage, "_first_locator", _fake_first_locator)
    assert await WhatsAppWebPage(page=None).session_state() == "logged_in"


@pytest.mark.asyncio
async def test_session_state_qr_required_quando_aggancia_solo_il_qr(monkeypatch):
    """Funzionale 15."""
    from app.browser.whatsapp_page import WhatsAppWebPage

    chiamate = {"n": 0}

    async def _fake_first_locator(self, candidates, timeout_ms=4000):
        chiamate["n"] += 1
        return None if chiamate["n"] == 1 else (object(), "qr-selettore")

    monkeypatch.setattr(WhatsAppWebPage, "_first_locator", _fake_first_locator)
    assert await WhatsAppWebPage(page=None).session_state() == "qr_required"


@pytest.mark.asyncio
async def test_session_state_unknown_quando_non_aggancia_nulla(monkeypatch):
    """Funzionale 16: una schermata non riconosciuta non e' una sessione
    valida."""
    from app.browser.whatsapp_page import WhatsAppWebPage

    async def _fake_first_locator(self, candidates, timeout_ms=4000):
        return None

    monkeypatch.setattr(WhatsAppWebPage, "_first_locator", _fake_first_locator)
    assert await WhatsAppWebPage(page=None).session_state() == "unknown"


# ---------------------------------------------------------------------------
# Funzionale D (17-18) -- open_chat: solo ricerca, mai deep-link.
# ---------------------------------------------------------------------------

class _FakeKeyboardOpenChat:
    def __init__(self):
        self.typed: list[str] = []
        self.pressed: list[str] = []

    async def type(self, text: str):
        self.typed.append(text)

    async def press(self, key: str):
        self.pressed.append(key)


class _RigaRisultato:
    """Una riga [role='row'] nei risultati di ricerca: fa sia da .first
    (per il controllo 'ci sono risultati') sia da elemento .nth(i)."""
    def __init__(self, testo: str, non_letto: bool = False):
        self._testo = testo
        self._non_letto = non_letto
        self.clicked = False

    async def wait_for(self, state="visible", timeout=4000):
        pass  # sempre visibile nella finta: qui si testa il ROUTING per riga

    async def inner_text(self):
        return self._testo

    async def click(self):
        self.clicked = True

    def locator(self, selettore: str):
        if selettore == sel.UNREAD_BADGE:
            return _FakeSimpleLocator(esiste=self._non_letto, count=1 if self._non_letto else 0)
        raise AssertionError(f"selettore non atteso sulla riga finta: {selettore}")


class _FakeRowsLocator:
    def __init__(self, righe: list[_RigaRisultato]):
        self._righe = righe

    @property
    def first(self):
        return self._righe[0]

    async def count(self):
        return len(self._righe)

    def nth(self, i: int):
        return self._righe[i]


class _FakeSearchBox:
    """La casella di ricerca: sempre svuotabile e a fuoco (scenario 'buono'
    -- i test su _svuota_ricerca/focus-perso sono fuori scope di questo
    batch, non richiesti dalla lista funzionale/adversarial)."""
    @property
    def first(self):
        return self

    async def wait_for(self, state="visible", timeout=4000):
        pass

    async def click(self):
        pass

    async def inner_text(self):
        return ""  # ricerca sempre svuotata con successo

    async def evaluate(self, _script):
        return True  # fuoco ancora sulla ricerca


class _FakeSimpleLocator:
    """Locator a esito singolo (COMPOSER, MSG_ROW): esiste o no nella pagina
    finta."""
    def __init__(self, esiste: bool, count: int = 0):
        self._esiste = esiste
        self._count = count

    @property
    def first(self):
        return self

    async def wait_for(self, state="visible", timeout=4000):
        if not self._esiste:
            raise TimeoutError("non trovato nella pagina finta")

    async def count(self):
        return self._count


class FakeOpenChatPage:
    """Pagina finta per open_chat/_apri_chat_da_risultati. NESSUN metodo
    goto: se open_chat lo chiamasse (deep-link vietato da SDD 6.4 punto 1,
    nemmeno come fallback) il test fallisce con un errore diagnostico
    invece di passare per sbaglio."""

    def __init__(self, righe_ricerca: list[str], composer_esiste: bool, msg_row_count: int = 0,
                 non_letti: set[int] = frozenset()):
        self.keyboard = _FakeKeyboardOpenChat()
        self._search_box = _FakeSearchBox()
        self._rows = _FakeRowsLocator([_RigaRisultato(t, non_letto=i in non_letti)
                                       for i, t in enumerate(righe_ricerca)])
        self._composer = _FakeSimpleLocator(esiste=composer_esiste)
        self._msgrow = _FakeSimpleLocator(esiste=msg_row_count > 0, count=msg_row_count)

    @property
    def rows(self) -> list[_RigaRisultato]:
        return self._rows._righe

    def locator(self, selettore: str):
        if selettore in sel.SEARCH:
            return self._search_box
        if selettore == sel.ROW:
            return self._rows
        if selettore in sel.COMPOSER:
            return self._composer
        if selettore == sel.MSG_ROW:
            return self._msgrow
        raise AssertionError(f"selettore non atteso in questa pagina finta: {selettore}")

    async def wait_for_timeout(self, _ms):
        pass

    async def goto(self, *_a, **_k):
        raise AssertionError(
            "open_chat NON deve MAI chiamare page.goto: e' un deep-link "
            "(wa.me/api.whatsapp.com/send?phone), vietato da SDD 6.4 punto 1, "
            "nemmeno come fallback -- su un numero senza chat ne creerebbe una nuova"
        )


@pytest.mark.asyncio
async def test_open_chat_cerca_non_deep_linka_e_apre_la_riga_sotto_chat(monkeypatch):
    """Funzionale 17+18. Nessun open_chat.goto (vietato); la riga cliccata e'
    quella SOTTO l'intestazione 'Chat' (indice 1), mai la riga 0; ok riflette
    solo il composer, signal la cronologia per intero -- qui assente:
    OpenResult(ok=True, signal='nessuna-cronologia:...') e' uno stato legale,
    e' M3 a decidere se bloccare l'invio."""
    from app.browser.whatsapp_page import WhatsAppWebPage

    _sleep_reale = asyncio.sleep   # catturare PRIMA del patch, o e' ricorsione infinita
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: _sleep_reale(0))

    page = FakeOpenChatPage(
        righe_ricerca=["Chat", "Mario Rossi"],
        composer_esiste=True,
        msg_row_count=0,
    )
    risultato = await WhatsAppWebPage(page).open_chat("393421460077")

    assert risultato.ok is True
    assert risultato.signal.startswith("nessuna-cronologia:")
    assert page.rows[0].clicked is False   # mai la riga 0 (intestazione 'Chat')
    assert page.rows[1].clicked is True    # la riga SOTTO la sezione 'Chat'

    # La ricerca digitata deve essere il numero, al netto dei typo corretti
    # (stesso pattern di verifica di test_human_input.py).
    battuti = "".join(page.keyboard.typed)
    backspace = page.keyboard.pressed.count("Backspace")
    assert len(battuti) - backspace == len("393421460077")


@pytest.mark.asyncio
async def test_open_chat_riporta_non_letto_se_il_badge_cera_prima_del_click(monkeypatch):
    """Backfill 21/08: chi apre una chat per un'operazione una tantum vuole
    sapere se era da leggere PRIMA di aprirla (che la marca letta). Il badge
    si legge sulla riga di risultato PRIMA del click, non dopo."""
    from app.browser.whatsapp_page import WhatsAppWebPage

    _sleep_reale = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: _sleep_reale(0))

    page = FakeOpenChatPage(
        righe_ricerca=["Chat", "Mario Rossi"], composer_esiste=True, non_letti={1})
    risultato = await WhatsAppWebPage(page).open_chat("393421460077")

    assert risultato.era_non_letto is True


@pytest.mark.asyncio
async def test_open_chat_non_letto_falso_se_il_badge_non_cera(monkeypatch):
    from app.browser.whatsapp_page import WhatsAppWebPage

    _sleep_reale = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: _sleep_reale(0))

    page = FakeOpenChatPage(
        righe_ricerca=["Chat", "Mario Rossi"], composer_esiste=True)
    risultato = await WhatsAppWebPage(page).open_chat("393421460077")

    assert risultato.era_non_letto is False


@pytest.mark.asyncio
async def test_apri_chat_da_risultati_senza_sezione_chat_non_clicca_un_gruppo():
    """Funzionale 17 (parte 'mai un gruppo'). Sotto 'Gruppi in comune' ci
    sono GRUPPI, fuori perimetro: se la sezione 'Chat' non c'e' proprio, il
    metodo non clicca NIENTE, non ripiega su un gruppo."""
    from app.browser.whatsapp_page import WhatsAppWebPage

    page = FakeOpenChatPage(righe_ricerca=["Gruppi in comune", "Gruppo Famiglia"],
                             composer_esiste=True)
    aperto, nota, era_non_letto = await WhatsAppWebPage(page)._apri_chat_da_risultati()

    assert aperto is False
    assert "nessuna-sezione-chat" in nota
    assert era_non_letto is False
    assert all(r.clicked is False for r in page.rows)


# ---------------------------------------------------------------------------
# Funzionale D-19 -- load_history: rotellina, minimo, esaurimento.
# ---------------------------------------------------------------------------

class _FakeMouseLoadHistory:
    def __init__(self):
        self.wheel_calls = 0

    async def move(self, _x, _y):
        pass

    async def wheel(self, _dx, _dy):
        self.wheel_calls += 1


class _FakeMsgRowCounter:
    """count() ritorna il prossimo valore di una sequenza prefissata: uno
    per la lettura iniziale (n_prima) e uno per ogni giro di rotellina."""
    def __init__(self, sequenza: list[int]):
        self._sequenza = iter(sequenza)
        self._ultimo = sequenza[0]

    async def count(self):
        try:
            self._ultimo = next(self._sequenza)
        except StopIteration:
            pass
        return self._ultimo


class _FakeMainBox:
    def __init__(self, box):
        self._box = box

    async def bounding_box(self):
        return self._box


class FakeLoadHistoryPage:
    def __init__(self, sequenza_conteggi: list[int], main_box):
        self.mouse = _FakeMouseLoadHistory()
        self._msgrow = _FakeMsgRowCounter(sequenza_conteggi)
        self._main = _FakeMainBox(main_box)

    def locator(self, selettore: str):
        if selettore == sel.MSG_ROW:
            return self._msgrow
        if selettore == sel.MAIN:
            return self._main
        raise AssertionError(f"selettore non atteso in questa pagina finta: {selettore}")


@pytest.mark.asyncio
async def test_load_history_scrolla_con_rotellina_fino_al_minimo(monkeypatch):
    from app.browser.whatsapp_page import WhatsAppWebPage

    _sleep_reale = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: _sleep_reale(0))

    page = FakeLoadHistoryPage([0, 30, 60, 90], main_box={"x": 0, "y": 0, "width": 800, "height": 600})
    info = await WhatsAppWebPage(page).load_history(minimo=80)

    assert info.ok is True
    assert info.before == 0
    assert info.after == 90
    assert info.rounds == 3
    assert info.exhausted is False
    assert page.mouse.wheel_calls == 3   # mai scrollTop: solo mouse.wheel


@pytest.mark.asyncio
async def test_load_history_esaurita_dopo_tre_giri_fermi(monkeypatch):
    from app.browser.whatsapp_page import WhatsAppWebPage

    _sleep_reale = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: _sleep_reale(0))

    # Conteggio che non si muove mai: la chat e' finita (inizio conversazione).
    page = FakeLoadHistoryPage([5, 5, 5, 5], main_box={"x": 0, "y": 0, "width": 800, "height": 600})
    info = await WhatsAppWebPage(page).load_history(minimo=80)

    assert info.exhausted is True
    assert info.rounds == 3
    assert page.mouse.wheel_calls == 3


@pytest.mark.asyncio
async def test_load_history_ok_false_senza_bounding_box():
    from app.browser.whatsapp_page import WhatsAppWebPage

    page = FakeLoadHistoryPage([12], main_box=None)
    info = await WhatsAppWebPage(page).load_history(minimo=80)

    assert info.ok is False
    assert info.before == 12
    assert info.after == 12
    assert info.rounds == 0
    assert page.mouse.wheel_calls == 0


# ---------------------------------------------------------------------------
# Funzionale F-32 -- non-regressione Instagram: namespacing del lock wa_.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_account_lock_namespace_wa_non_collide_con_instagram():
    """Instagram e WhatsApp condividono lo stesso dizionario dei lock
    (app.browser.context_manager._account_locks, usato sia da InstagramPage
    che da wa_session._get_wa_lock). Il prefisso wa_ esiste per non
    collidere con gli account_id Instagram nello stesso spazio: se
    _get_account_lock('x') e _get_account_lock('wa_x') fossero lo stesso
    lock, un login IG e un login WA sullo stesso 'x' si serializzerebbero a
    vicenda senza motivo."""
    from app.browser.context_manager import _get_account_lock

    lock_ig = _get_account_lock("x")
    lock_wa = _get_account_lock("wa_x")
    assert lock_ig is not lock_wa
