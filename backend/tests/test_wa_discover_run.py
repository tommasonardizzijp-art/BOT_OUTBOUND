# backend/tests/test_wa_discover_run.py
"""L'orchestratore del giro Fase A: gate, scorciatoia titolo=numero, salvataggio
condizionato dall'esito di apertura, kill-switch, confronto raccolto/dichiarato.

Confine di test deliberato: `sidebar.scan_sidebar/scorri_sidebar/totale_dichiarato`
girano DAVVERO contro una pagina finta (hanno la loro suite in
test_wa_discover_sidebar.py, qui si esercita solo l'integrazione); `pannello.
apri_e_leggi` invece si monkeypatcha con esiti pre-cotti -- ha gia' la sua suite
in test_wa_discover_pannello.py, e simulare qui anche i suoi click/evaluate
interni testerebbe di nuovo il DOM del pannello invece delle decisioni
dell'orchestratore, che sono l'oggetto di questo file.
"""
import pytest

from app.services import bot_state_service, wa_discover_run
from app.services.wa_discover import classifica, pannello, sidebar
from app.utils.phone_pseudonym import hmac_phone

from tests.test_wa_discover_modello import _scoperte_di, numero_wa  # noqa: F401


async def _non_deve_essere_chiamato(*args, **kwargs):
    raise AssertionError("chiamata inattesa: questo ramo non doveva essere raggiunto")


def _e_query_scan(script: str) -> bool:
    return "righe:" in script


def _e_query_stato_pane(script: str) -> bool:
    return "alFondo" in script


class _FakeLocator:
    """Il locator di una voce cliccabile. `esiste` False simula una UI in cui
    quella voce non c'e' (lingua diversa, layout cambiato)."""

    def __init__(self, pagina, esiste):
        self._pagina = pagina
        self._esiste = esiste

    @property
    def first(self):
        return self

    async def count(self):
        return 1 if self._esiste else 0

    async def click(self, **kwargs):
        if self._esiste:
            self._pagina.impostazioni_aperte += 1


class _FakePage:
    """Pagina finta per la sidebar, stesso principio di _FakePageSidebar in
    test_wa_discover_sidebar.py. `righe_per_scroll` e' una lista di liste di
    righe grezze: una voce per ogni passo di scorrimento -- la prima e' cio'
    che scan_sidebar vede PRIMA di scorrere, le successive dopo ogni
    scorri_sidebar().

    Il totale dichiarato (aria-rowcount) si legge con la STESSA query JS di
    scorri_sidebar (_JS_STATO_PANE): la prima chiamata in assoluto e'
    sempre `totale_dichiarato` (letta una volta sola, senza scorrere);
    ogni scorri_sidebar successivo ne fa DUE (prima/dopo) e solo la
    seconda avanza il passo simulato.
    """

    class _Mouse:
        async def move(self, *a, **k):
            pass

        async def wheel(self, *a, **k):
            pass

    class _Keyboard:
        async def press(self, *a, **k):
            pass

    def __init__(self, *, righe_per_scroll, testo_pagina="", rowcount=None,
                viewport_h=800, drawer_aperti=None):
        self._righe_per_scroll = list(righe_per_scroll)
        self._passo = 0
        self._totale_letto = False
        self._aspetta_dopo = False
        self.testo_pagina = testo_pagina
        self.rowcount = rowcount
        self.viewport_h = viewport_h
        self.mouse = self._Mouse()
        self.keyboard = self._Keyboard()
        self.impostazioni_aperte = 0
        # Pannelli che coprono la lista chat: quando ce n'e' uno, la riga sotto
        # il puntatore non appartiene alla lista e nessun click la raggiunge --
        # sul DOM reale lo scan raccoglieva 5 righe su 65 (collaudo 11/08).
        self.drawer_aperti = list(drawer_aperti or [])

    def locator(self, selettore):
        """Simula la voce Impostazioni: la percentuale di sincronizzazione vive
        li' dentro, non nel body (verificato PoC-5). Un fake che rispondesse
        senza richiedere il click lascerebbe passare un gate cieco."""
        return _FakeLocator(self, "Impostazioni" in selettore or "Settings" in selettore)

    async def evaluate(self, script, *args):
        if "elementFromPoint" in script:
            # "La lista e' utilizzabile?": falso quando un pannello la copre.
            return not self.drawer_aperti
        if "textContent" in script and "children.length" in script:
            # I testi della pagina, letti da leggi_percentuale DOPO il click su
            # Impostazioni: prima di quel click il pannello non c'e'.
            if not self.impostazioni_aperte:
                return []
            return [r for r in (self.testo_pagina or "").splitlines() if r.strip()]
        if "body.innerText" in script:
            # Volutamente VUOTO: la percentuale di sincronizzazione non sta nel
            # body, sta dentro Impostazioni (verificato PoC-5). Un fake che la
            # restituisse anche da qui renderebbe verdi i test pure con un gate
            # che non apre niente -- ed e' esattamente l'errore che c'era.
            return ""
        if _e_query_scan(script):
            righe = self._righe_per_scroll[min(self._passo, len(self._righe_per_scroll) - 1)]
            return {"viewport_h": self.viewport_h, "righe": righe}
        if _e_query_stato_pane(script):
            if not self._totale_letto:
                self._totale_letto = True
            elif not self._aspetta_dopo:
                self._aspetta_dopo = True          # lettura "prima" dello scroll
            else:
                self._passo += 1                    # lettura "dopo": avanza il passo
                self._aspetta_dopo = False
            al_fondo = self._passo >= len(self._righe_per_scroll)
            return {"altezza": 1000, "alFondo": al_fondo, "rowcount": self.rowcount,
                    "left": 0, "top": 0, "w": 400, "h": 800, "clientHeight": 800}
        raise AssertionError(f"query JS non riconosciuta dal fake: {script[:60]!r}")

    async def wait_for_timeout(self, ms):
        return None


# ── Gate di sincronizzazione ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gate_sotto_soglia_non_scansiona(monkeypatch, db_session, numero_wa):
    """Sotto soglia lo scan non deve nemmeno cominciare: nessuna lettura
    della sidebar, nessuna scrittura."""
    monkeypatch.setattr(sidebar, "scan_sidebar", _non_deve_essere_chiamato)
    page = _FakePage(
        righe_per_scroll=[[]],
        testo_pagina="Sincronizzazione dei messaggi precedenti in corso\nCompletata al 20%",
    )
    esito = await wa_discover_run._esegui_scan(
        page, db=db_session, tenant_id=numero_wa.tenant_id, number_id=numero_wa.id)

    assert esito["motivo"] == "sync_sotto_soglia"
    assert esito["salvate"] == 0 and esito["aggiornate"] == 0
    assert esito["dichiarato"] is None, "il gate deve fermarsi PRIMA di leggere il totale"
    assert await _scoperte_di(db_session, numero_wa.id) == []


@pytest.mark.asyncio
async def test_gate_sopra_soglia_scansiona(db_session, numero_wa):
    page = _FakePage(
        righe_per_scroll=[[{"position": 0, "titolo": "+39 342 146 0077", "top": 200}]],
        testo_pagina="Sincronizzazione dei messaggi precedenti in corso\nCompletata al 90%",
        rowcount=1,
    )
    esito = await wa_discover_run._esegui_scan(
        page, db=db_session, tenant_id=numero_wa.tenant_id, number_id=numero_wa.id)

    assert esito["motivo"] != "sync_sotto_soglia"
    assert esito["salvate"] == 1


@pytest.mark.asyncio
async def test_gate_percentuale_ignota_scansiona_comunque(db_session, numero_wa):
    """None non e' zero: un pannello che non espone la percentuale non deve
    bloccare la Fase A nel caso normale (decisione di sincronizzazione.py)."""
    page = _FakePage(
        righe_per_scroll=[[{"position": 0, "titolo": "+39 342 146 0077", "top": 200}]],
        testo_pagina="Chat\nImpostazioni\nPrivacy",
        rowcount=1,
    )
    esito = await wa_discover_run._esegui_scan(
        page, db=db_session, tenant_id=numero_wa.tenant_id, number_id=numero_wa.id)

    assert esito["motivo"] != "sync_sotto_soglia"
    assert esito["salvate"] == 1


# ── Il 39% che non costa nulla ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_riga_titolo_numero_salvata_senza_aprire_il_pannello(
        monkeypatch, db_session, numero_wa):
    """La riga il cui titolo E' gia' il numero si salva dal solo titolo:
    pannello.apri_e_leggi non deve MAI essere chiamato per lei -- e' il
    risparmio principale sui 5,3s/chat (Task 5/PoC-4)."""
    monkeypatch.setattr(pannello, "apri_e_leggi", _non_deve_essere_chiamato)
    page = _FakePage(
        righe_per_scroll=[[{"position": 0, "titolo": "+39 342 146 0077", "top": 200}]],
        rowcount=1,
    )
    esito = await wa_discover_run._esegui_scan(
        page, db=db_session, tenant_id=numero_wa.tenant_id, number_id=numero_wa.id)

    assert esito["salvate"] == 1
    trovate = await _scoperte_di(db_session, numero_wa.id)
    assert len(trovate) == 1
    assert trovate[0].phone_hmac == hmac_phone("393421460077")
    assert trovate[0].numero_leggibile is True
    assert trovate[0].tipo_chat == classifica.TIPO_INDIVIDUALE


# ── Righe che passano dal pannello ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_riga_verificata_senza_numero_si_salva_marcata(
        monkeypatch, db_session, numero_wa):
    """Il 100% dei gruppi (PoC-4): titolo verificato, numero non leggibile.
    Si salva comunque, marcata numero_leggibile=False -- spec 5.3/5.4."""
    async def _fake_apri(page, titolo_atteso):
        return pannello.EsitoApertura(pannello.ESITO_VERIFICATA, None, "12 partecipanti")

    monkeypatch.setattr(pannello, "apri_e_leggi", _fake_apri)
    page = _FakePage(
        righe_per_scroll=[[{"position": 0, "titolo": "Famiglia Rossi", "top": 200}]],
        rowcount=1,
    )
    esito = await wa_discover_run._esegui_scan(
        page, db=db_session, tenant_id=numero_wa.tenant_id, number_id=numero_wa.id)

    assert esito["salvate"] == 1
    trovate = await _scoperte_di(db_session, numero_wa.id)
    assert len(trovate) == 1
    assert trovate[0].chat_title == "Famiglia Rossi"
    assert trovate[0].numero_leggibile is False
    assert trovate[0].phone_hmac is None
    assert trovate[0].tipo_chat == classifica.TIPO_GRUPPO


@pytest.mark.asyncio
async def test_esito_non_verificato_non_scrive_nulla(monkeypatch, db_session, numero_wa):
    """La lista si e' riordinata e si e' aperta un'altra persona: NESSUNA
    scrittura, mai un dato attribuito alla persona sbagliata -- si ritenta
    al giro dopo."""
    async def _fake_apri(page, titolo_atteso):
        return pannello.EsitoApertura(pannello.ESITO_NON_VERIFICATA, None, "")

    monkeypatch.setattr(pannello, "apri_e_leggi", _fake_apri)
    page = _FakePage(
        righe_per_scroll=[[{"position": 0, "titolo": "Fulvio", "top": 200}]],
        rowcount=1,
    )
    esito = await wa_discover_run._esegui_scan(
        page, db=db_session, tenant_id=numero_wa.tenant_id, number_id=numero_wa.id)

    assert esito["salvate"] == 0 and esito["aggiornate"] == 0
    assert esito["non_verificate"] == 1
    assert await _scoperte_di(db_session, numero_wa.id) == []


@pytest.mark.asyncio
async def test_riga_assente_non_scrive_nulla(monkeypatch, db_session, numero_wa):
    """La riga e' sparita dal DOM virtualizzato prima del click: nessuna
    scrittura, come per non_verificata."""
    async def _fake_apri(page, titolo_atteso):
        return pannello.EsitoApertura(pannello.ESITO_RIGA_ASSENTE, None, "")

    monkeypatch.setattr(pannello, "apri_e_leggi", _fake_apri)
    page = _FakePage(
        righe_per_scroll=[[{"position": 0, "titolo": "Fulvio", "top": 200}]],
        rowcount=1,
    )
    esito = await wa_discover_run._esegui_scan(
        page, db=db_session, tenant_id=numero_wa.tenant_id, number_id=numero_wa.id)

    assert esito["non_verificate"] == 1
    assert await _scoperte_di(db_session, numero_wa.id) == []


# ── Kill-switch ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_kill_switch_a_meta_giro_si_ferma(monkeypatch, db_session, numero_wa):
    """`is_wa_halted` diventa vero a meta' lotto: lo scan si ferma SUBITO,
    non alla prossima riga passiva. Le righe gia' processate restano
    salvate (nessun rollback), quelle dopo lo stop non vengono toccate."""
    chiamate = {"n": 0}

    async def _fake_halted():
        chiamate["n"] += 1
        return chiamate["n"] > 2  # le prime due letture "non fermo", poi si ferma

    monkeypatch.setattr(bot_state_service, "is_wa_halted", _fake_halted)

    righe = [{"position": i, "titolo": f"+39 342 146 007{i}", "top": 200} for i in range(6)]
    page = _FakePage(righe_per_scroll=[righe], rowcount=6)

    esito = await wa_discover_run._esegui_scan(
        page, db=db_session, tenant_id=numero_wa.tenant_id, number_id=numero_wa.id)

    assert esito["motivo"] == "wa_halted"
    trovate = await _scoperte_di(db_session, numero_wa.id)
    assert 0 < len(trovate) < 6, "deve essersi fermato a meta', non aver processato tutto"


async def _sempre_fermo():
    return True


@pytest.mark.asyncio
async def test_kill_switch_prima_di_iniziare_non_scansiona(monkeypatch, db_session, numero_wa):
    monkeypatch.setattr(bot_state_service, "is_wa_halted", _sempre_fermo)
    page = _FakePage(
        righe_per_scroll=[[{"position": 0, "titolo": "+39 342 146 0077", "top": 200}]],
        rowcount=1,
    )
    esito = await wa_discover_run._esegui_scan(
        page, db=db_session, tenant_id=numero_wa.tenant_id, number_id=numero_wa.id)

    assert esito["motivo"] == "wa_halted"
    assert await _scoperte_di(db_session, numero_wa.id) == []


# ── Confronto raccolto vs dichiarato ────────────────────────────────────────

@pytest.mark.asyncio
async def test_scarto_grosso_tra_raccolto_e_dichiarato_e_segnalato(
        monkeypatch, db_session, numero_wa):
    """Sidebar piantata: la stessa riga ricompare a ogni scorrimento (nessuna
    riga nuova), il fondo lista non si raggiunge mai. Il giro si arrende
    dopo MAX_SCROLL_SENZA_NUOVE_RIGHE, e con un dichiarato molto piu' alto
    del raccolto l'evento finale deve dirlo -- non "completata"."""
    eventi = []
    monkeypatch.setattr(wa_discover_run, "emit_event",
                        lambda *a, **k: eventi.append((a, k)))

    riga = [{"position": 0, "titolo": "+39 342 146 0077", "top": 200}]
    page = _FakePage(righe_per_scroll=[riga] * 10, rowcount=100)

    esito = await wa_discover_run._esegui_scan(
        page, db=db_session, tenant_id=numero_wa.tenant_id, number_id=numero_wa.id)

    assert esito["dichiarato"] == 100
    assert esito["salvate"] + esito["aggiornate"] == 1
    assert esito["motivo"] in ("fermato_dopo_stallo", "raccolta_parziale")

    azioni = [a[1] for a, _ in eventi]
    livelli = [k.get("level") for _, k in eventi]
    assert "wa_discover_parziale" in azioni, f"eventi emessi: {eventi}"
    assert "warn" in livelli


@pytest.mark.asyncio
async def test_scarto_piccolo_non_e_segnalato_come_parziale(db_session, numero_wa):
    """Il caso normale: raccolto vicino al dichiarato -> evento di
    completamento, non di raccolta parziale."""
    eventi = []
    righe = [{"position": i, "titolo": f"+39 342 146 007{i}", "top": 200} for i in range(5)]
    page = _FakePage(righe_per_scroll=[righe], rowcount=5)

    esito = await wa_discover_run._esegui_scan(
        page, db=db_session, tenant_id=numero_wa.tenant_id, number_id=numero_wa.id)

    assert esito["dichiarato"] == 5
    assert esito["salvate"] == 5
    assert esito["motivo"] == "completato"


# ── Rinnovo del lucchetto durante il giro ───────────────────────────────────

@pytest.mark.asyncio
async def test_rinnova_il_lock_solo_se_il_token_e_dato(monkeypatch, db_session, numero_wa):
    """Con lock_token=None (default nei test sopra) wa_profile_lock.renew non
    deve mai essere chiamato -- e' cosi' che quei test possono girare senza
    Redis. Con un token, deve esserlo."""
    from app.services import wa_profile_lock

    chiamate = []

    async def _fake_renew(number_id, token):
        chiamate.append((number_id, token))
        return True

    monkeypatch.setattr(wa_profile_lock, "renew", _fake_renew)
    page = _FakePage(
        righe_per_scroll=[[{"position": 0, "titolo": "+39 342 146 0077", "top": 200}]],
        rowcount=1,
    )

    await wa_discover_run._esegui_scan(
        page, db=db_session, tenant_id=numero_wa.tenant_id, number_id=numero_wa.id,
        lock_token=None)
    assert chiamate == []

    await wa_discover_run._esegui_scan(
        page, db=db_session, tenant_id=numero_wa.tenant_id, number_id=numero_wa.id,
        lock_token="tok-123")
    assert chiamate == [(numero_wa.id, "tok-123")]


@pytest.mark.asyncio
async def test_non_si_scansiona_con_la_sidebar_coperta(monkeypatch, db_session, numero_wa):
    """Il difetto che ha fatto fallire il collaudo dell'11/08, dal lato
    dell'orchestratore.

    Con un pannello aperto sopra la lista, la sidebar reale espone 5 righe su 65
    e nessun click apre una chat -- ma nulla solleva un errore: il giro finisce
    "dopo stallo" con 1 chat su 291 e sembra un problema di scorrimento, mentre
    e' una tenda davanti alla lista. Meglio non partire affatto che raccogliere
    una frazione e dichiararla completa.
    """
    monkeypatch.setattr(sidebar, "scan_sidebar", _non_deve_essere_chiamato)
    page = _FakePage(
        righe_per_scroll=[[{"position": 0, "titolo": "Fulvio", "top": 200}]],
        testo_pagina="Sincronizzazione dei messaggi precedenti in corso\nCompletata al 90%",
        rowcount=291,
        drawer_aperti=["drawer-fullscreen", "drawer-left", "drawer-title-privacy"],
    )
    esito = await wa_discover_run._esegui_scan(
        page, db=db_session, tenant_id=numero_wa.tenant_id, number_id=numero_wa.id)

    assert esito["motivo"] == "sidebar_coperta"
    assert esito["salvate"] == 0
    assert await _scoperte_di(db_session, numero_wa.id) == []
