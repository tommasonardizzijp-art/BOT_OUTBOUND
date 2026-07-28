import asyncio
import itertools
import os
import re
import shutil
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from loguru import logger as loguru_logger

from app.config import settings
from app.models.tenant import Tenant
from app.models.wa import WaNumber, WaNumberStatus
from app.services import wa_session
from app.services.wa_session import (
    _get_wa_lock,
    _open_wa_browser,
    assisted_login,
    check_session,
    profile_dir_for,
    stato_da_segnale,
)
from app.utils.phone_pseudonym import hmac_phone


@pytest.mark.parametrize("segnale,atteso", [
    ("logged_in",   WaNumberStatus.active),
    ("qr_required", WaNumberStatus.qr_required),
    ("unknown",     WaNumberStatus.disconnected),
])
def test_stato_da_segnale(segnale, atteso):
    assert stato_da_segnale(segnale) == atteso


def test_schermata_ignota_non_diventa_active():
    """'unknown' e' una schermata che non abbiamo riconosciuto: un interstitial,
    un aggiornamento, un ban. Mapparla su active farebbe partire gli invii
    contro una pagina che non e' WhatsApp."""
    assert stato_da_segnale("unknown") != WaNumberStatus.active


def test_profile_dir_e_per_numero():
    a, b = profile_dir_for("num-a"), profile_dir_for("num-b")
    assert a != b
    assert a.name == "wa_num-a"


# ---------------------------------------------------------------------------
# Infrastruttura di test per check_session / assisted_login (test 23-31 +
# adversarial E/G/H/I). Tutto con doppi finti: nessun browser vero, nessun
# accesso a WhatsApp. Doppio finto per il LAUNCHER (patchright), non per
# _open_wa_browser: il lock per-numero che protegge il profilo vive dentro
# _open_wa_browser, e i test di concorrenza (adversarial 24-26) devono
# esercitare quel lock vero, non uno bypassato da un finto piu' a monte.
# ---------------------------------------------------------------------------

_numero_counter = itertools.count(1)

# Suffisso casuale per SESSIONE pytest, non solo per test. Il contatore da
# solo non basta: wa_numbers.phone_hmac e' UNIQUE GLOBALE e il DB sqlite di
# test e' un file CONDIVISO, quindi due suite lanciate insieme ripartono
# entrambe dal contatore 1, generano lo stesso numero e la seconda si prende
# un "UNIQUE constraint failed: wa_numbers.phone_hmac".
#
# Costato un'ora il 28/07: 22 test rossi che sembravano una regressione del
# modulo ed erano due pytest sovrapposti sullo stesso file. La regola resta
# "una suite alla volta", ma un fixture che si rompe solo quando qualcuno la
# viola in un altro processo produce un rosso che non nomina la propria causa.
_SESSIONE = f"{os.getpid() % 100:02d}"


def _numero_test() -> str:
    """Numero di test con prefisso dedicato (mai un numero reale a DB), unico
    anche fra sessioni pytest concorrenti sullo stesso file sqlite."""
    return f"39900{_SESSIONE}{next(_numero_counter):04d}"


async def _reload(number_id: str) -> WaNumber:
    """Rilettura FRESCA dalla stessa AsyncSessionLocal usata dal codice sotto
    test (non db_session, che e' un engine separato con rollback): serve a
    vedere davvero cio' che check_session/assisted_login hanno scritto."""
    from sqlalchemy import select

    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        r = await db.execute(select(WaNumber).where(WaNumber.id == number_id))
        return r.scalar_one()


@pytest_asyncio.fixture
async def wa_number_factory():
    """Crea un WaNumber (+ Tenant) via la AsyncSessionLocal REALE (app.database),
    con commit vero: check_session/assisted_login aprono una connessione
    separata dalla fixture e devono vedere la riga -- un flush() su db_session
    non basterebbe (transazioni diverse). Pulisce la cartella profilo a fine
    test: _open_wa_browser scrive su disco per davvero anche con i finti."""
    from app.database import AsyncSessionLocal

    creati: list[str] = []

    async def _make(*, status: WaNumberStatus = WaNumberStatus.pending_qr,
                     proxy_url: str | None = None) -> str:
        async with AsyncSessionLocal() as db:
            t = Tenant(id=str(uuid.uuid4()), name="QA wa_session")
            db.add(t)
            await db.flush()
            n = WaNumber(
                id=str(uuid.uuid4()), tenant_id=t.id, label="QA numero",
                phone_hmac=hmac_phone(_numero_test()), encrypted_phone="x",
                status=status, proxy_url=proxy_url,
            )
            db.add(n)
            await db.commit()
            creati.append(n.id)
            return n.id

    yield _make

    for number_id in creati:
        shutil.rmtree(profile_dir_for(number_id), ignore_errors=True)


@pytest.fixture
def numero_id_usa_e_getta():
    """number_id 'a perdere' per i test che chiamano _open_wa_browser
    direttamente, senza una riga WaNumber a DB (es. proxy malformato, che
    fallisce prima di aver bisogno del DB). Pulisce la cartella profilo."""
    nid = f"qa-{uuid.uuid4()}"
    yield nid
    shutil.rmtree(profile_dir_for(nid), ignore_errors=True)


class _LaunchProbe:
    """Il doppio finto del LAUNCHER (patchright). Conta le sovrapposizioni fra
    l'apertura (launch_persistent_context) e la chiusura (context.close()):
    se il lock per-numero funziona, max_concurrent non supera mai 1 per lo
    stesso profilo. launch_delay + un await reale dentro launch() sono cio'
    che rende la concorrenza vera (senza un await che cede il controllo,
    asyncio non farebbe mai girare l'altra coroutine e il test passerebbe
    per finta anche con un lock rotto)."""

    def __init__(self, launch_delay: float = 0.05, real_lock_file: Path | None = None):
        self.current = 0
        self.max_concurrent = 0
        self.launch_delay = launch_delay
        self.launch_count = 0
        self.close_count = 0
        self.launch_kwargs: list[dict] = []
        self.init_scripts: list[str] = []
        self.should_raise: Exception | None = None
        # adv24b: se impostato, il finto scrive un lock-file VERO su disco
        # (come farebbe Chromium mentre e' vivo) e lo tiene finche' non
        # chiude -- serve a esercitare la pulizia REALE di _open_wa_browser
        # (quella che tocca il filesystem), non solo il conteggio delle
        # sovrapposizioni sul launcher finto.
        self.real_lock_file = real_lock_file

    async def launch(self, **kwargs):
        if self.should_raise is not None:
            raise self.should_raise
        self.launch_count += 1
        self.current += 1
        self.max_concurrent = max(self.max_concurrent, self.current)
        self.launch_kwargs.append(kwargs)
        if self.real_lock_file is not None:
            self.real_lock_file.parent.mkdir(parents=True, exist_ok=True)
            self.real_lock_file.write_bytes(b"fake-chromium-active-lock")
        await asyncio.sleep(self.launch_delay)
        return _FakeContext(self)


class _FakePage:
    def __init__(self):
        self.goto_calls: list[str] = []

    async def goto(self, url, wait_until=None):
        self.goto_calls.append(url)


class _FakeContext:
    def __init__(self, probe: _LaunchProbe):
        self._probe = probe

    async def new_page(self):
        return _FakePage()

    async def add_init_script(self, script):
        self._probe.init_scripts.append(script)

    async def close(self):
        self._probe.close_count += 1
        self._probe.current -= 1
        if self._probe.real_lock_file is not None:
            self._probe.real_lock_file.unlink(missing_ok=True)


class _FakePlaywrightCM:
    def __init__(self, probe: _LaunchProbe):
        self._probe = probe

    async def __aenter__(self):
        chromium = SimpleNamespace(launch_persistent_context=self._probe.launch)
        return SimpleNamespace(chromium=chromium)

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _patch_launcher(monkeypatch, probe: _LaunchProbe) -> None:
    """Patcha patchright.async_api.async_playwright (il riferimento ORIGINALE
    non ci serve: _open_wa_browser lo re-importa a ogni chiamata con un
    import locale, quindi patchare l'attributo del modulo basta -- non e' un
    monkeypatch ricorsivo, e' un attributo di modulo diverso dalla funzione
    che lo chiama)."""
    import patchright.async_api as pw_api

    monkeypatch.setattr(pw_api, "async_playwright", lambda: _FakePlaywrightCM(probe))


class _FakeWaPage:
    """Sostituisce WhatsAppWebPage: session_state() controllabile dal test,
    nessun accesso DOM/browser vero."""

    def __init__(self, page, *, segnali, delay: float, on_call):
        self._page = page
        self._segnali = list(segnali)
        self._delay = delay
        self._on_call = on_call
        self.calls = 0

    async def session_state(self):
        self.calls += 1
        if self._on_call is not None:
            await self._on_call(self.calls)
        await asyncio.sleep(self._delay)
        if len(self._segnali) > 1:
            return self._segnali.pop(0)
        return self._segnali[0]


def _patch_wa_page(monkeypatch, *, segnali=("logged_in",), delay: float = 0.0,
                    on_call=None, istanze: list | None = None) -> None:
    def _factory(page):
        inst = _FakeWaPage(page, segnali=segnali, delay=delay, on_call=on_call)
        if istanze is not None:
            istanze.append(inst)
        return inst

    monkeypatch.setattr(wa_session, "WhatsAppWebPage", _factory)


# ---------------------------------------------------------------------------
# E. wa_session -- test funzionali 23-31
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e23_proxy_arriva_dal_db_al_launcher(wa_number_factory, monkeypatch):
    from app.browser.context_manager import parse_proxy_url

    proxy = "http://utente:segreto@203.0.113.5:8080"
    number_id = await wa_number_factory(proxy_url=proxy)
    probe = _LaunchProbe(launch_delay=0.0)
    _patch_launcher(monkeypatch, probe)
    _patch_wa_page(monkeypatch, segnali=["logged_in"])

    await check_session(number_id)

    assert probe.launch_kwargs[-1]["proxy"] == parse_proxy_url(proxy)


@pytest.mark.asyncio
async def test_e24_headless_giusto_per_il_mestiere_giusto(wa_number_factory, monkeypatch):
    n1 = await wa_number_factory()
    n2 = await wa_number_factory()
    probe = _LaunchProbe(launch_delay=0.0)
    _patch_launcher(monkeypatch, probe)
    _patch_wa_page(monkeypatch, segnali=["logged_in"])

    await check_session(n1)
    await assisted_login(n2, timeout_s=0)

    assert probe.launch_kwargs[0]["headless"] is True    # check_session
    assert probe.launch_kwargs[1]["headless"] is False   # assisted_login


@pytest.mark.asyncio
async def test_e25_check_session_scrive_davvero_su_wanumber(wa_number_factory, monkeypatch):
    number_id = await wa_number_factory()
    probe = _LaunchProbe(launch_delay=0.0)
    _patch_launcher(monkeypatch, probe)
    _patch_wa_page(monkeypatch, segnali=["logged_in"])

    await check_session(number_id)

    numero = await _reload(number_id)
    assert numero.status == WaNumberStatus.active
    assert numero.session_checked_at is not None
    assert numero.browser_profile == str(profile_dir_for(number_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("segnale,atteso", [
    ("logged_in",   WaNumberStatus.active),
    ("qr_required", WaNumberStatus.qr_required),
    ("unknown",     WaNumberStatus.disconnected),
])
async def test_e26_mappatura_segnale_stato_end_to_end(wa_number_factory, monkeypatch, segnale, atteso):
    number_id = await wa_number_factory()
    probe = _LaunchProbe(launch_delay=0.0)
    _patch_launcher(monkeypatch, probe)
    _patch_wa_page(monkeypatch, segnali=[segnale])

    stato = await check_session(number_id)
    assert stato == atteso

    numero = await _reload(number_id)
    assert numero.status == atteso


@pytest.mark.asyncio
async def test_e27a_lock_file_presente_viene_rimosso(monkeypatch, numero_id_usa_e_getta):
    probe = _LaunchProbe(launch_delay=0.0)
    _patch_launcher(monkeypatch, probe)
    profile_dir = profile_dir_for(numero_id_usa_e_getta)
    profile_dir.mkdir(parents=True, exist_ok=True)
    lock_files = ["SingletonLock", "SingletonCookie", "SingletonSocket"]
    for name in lock_files:
        (profile_dir / name).write_bytes(b"stale")

    async with _open_wa_browser(numero_id_usa_e_getta, headless=True, proxy_url=None):
        pass

    for name in lock_files:
        assert not (profile_dir / name).exists()
    assert probe.launch_count == 1


@pytest.mark.asyncio
async def test_e27b_lock_file_assente_nessun_errore(monkeypatch, numero_id_usa_e_getta):
    probe = _LaunchProbe(launch_delay=0.0)
    _patch_launcher(monkeypatch, probe)

    async with _open_wa_browser(numero_id_usa_e_getta, headless=True, proxy_url=None):
        pass  # nessuna eccezione: cartella profilo vuota o assente

    assert probe.launch_count == 1


@pytest.mark.asyncio
async def test_e27c_rimozione_lock_file_fallisce_warning_e_launch_procede(monkeypatch, numero_id_usa_e_getta):
    probe = _LaunchProbe(launch_delay=0.0)
    _patch_launcher(monkeypatch, probe)
    profile_dir = profile_dir_for(numero_id_usa_e_getta)
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "SingletonLock").write_bytes(b"stale")

    def _boom(path):
        raise OSError("simulato: permesso negato")

    monkeypatch.setattr(wa_session.os, "remove", _boom)

    messages: list[str] = []
    sink_id = loguru_logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        async with _open_wa_browser(numero_id_usa_e_getta, headless=True, proxy_url=None):
            pass
    finally:
        loguru_logger.remove(sink_id)

    assert probe.launch_count == 1  # il launch e' proceduto lo stesso
    assert any(numero_id_usa_e_getta in m for m in messages)


@pytest.mark.asyncio
async def test_e28_fingerprint_iniettato(wa_number_factory, monkeypatch):
    from app.browser.context_manager import _build_fingerprint_script
    from app.browser.fingerprint import get_fingerprint

    number_id = await wa_number_factory()
    probe = _LaunchProbe(launch_delay=0.0)
    _patch_launcher(monkeypatch, probe)
    _patch_wa_page(monkeypatch, segnali=["logged_in"])

    await check_session(number_id)

    fp = get_fingerprint(number_id)
    kw = probe.launch_kwargs[-1]
    assert kw["viewport"] == fp["viewport"]
    assert kw["user_agent"] == fp["user_agent"]
    assert kw["locale"] == fp["locale"]
    assert kw["timezone_id"] == fp["timezone_id"]
    assert probe.init_scripts[-1] == _build_fingerprint_script(fp)


@pytest.mark.asyncio
async def test_e29_assisted_login_esce_al_primo_logged_in(wa_number_factory, monkeypatch):
    number_id = await wa_number_factory()
    probe = _LaunchProbe(launch_delay=0.0)
    _patch_launcher(monkeypatch, probe)
    istanze: list = []
    _patch_wa_page(monkeypatch, segnali=["logged_in"], delay=0.01, istanze=istanze)

    t0 = time.perf_counter()
    stato = await assisted_login(number_id, timeout_s=180)
    elapsed = time.perf_counter() - t0

    assert stato == WaNumberStatus.active
    assert istanze[0].calls == 1  # un solo giro di polling
    # Se avesse aspettato un secondo giro avrebbe pagato ASSISTED_LOGIN_POLL_INTERVAL_S (2s).
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_e30_assisted_login_a_timeout_scaduto_non_solleva(wa_number_factory, monkeypatch):
    number_id = await wa_number_factory()
    probe = _LaunchProbe(launch_delay=0.0)
    _patch_launcher(monkeypatch, probe)
    _patch_wa_page(monkeypatch, segnali=["unknown"])

    stato = await assisted_login(number_id, timeout_s=0)

    assert stato == WaNumberStatus.disconnected
    numero = await _reload(number_id)
    assert numero.status == WaNumberStatus.disconnected


@pytest.mark.asyncio
async def test_e31_timeout_s_custom_rispettato(wa_number_factory, monkeypatch):
    number_id = await wa_number_factory()
    probe = _LaunchProbe(launch_delay=0.0)
    _patch_launcher(monkeypatch, probe)
    _patch_wa_page(monkeypatch, segnali=["qr_required"], delay=0.01)
    # Poll interval reale e' 2.0s: senza abbassarlo il test dovrebbe aspettare
    # almeno un giro intero. Costante isolata (non un numero nudo in mezzo al
    # test), stessa logica della guardia sulla regressione 8000/90000.
    poll_interval_di_test = 0.03
    monkeypatch.setattr(wa_session, "ASSISTED_LOGIN_POLL_INTERVAL_S", poll_interval_di_test)

    t0 = time.perf_counter()
    stato = await assisted_login(number_id, timeout_s=0.2)
    elapsed = time.perf_counter() - t0

    assert stato == WaNumberStatus.qr_required  # mappato dall'ultimo segnale visto
    assert elapsed < 1.0  # deadline calcolata su loop.time(), non un timeout dimenticato


# ---------------------------------------------------------------------------
# F. Non-regressione Instagram -- item 32 (solo la parte di competenza di
# wa_session: il namespacing del lock. La suite IG completa la corre il team
# lead a fine batch).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_f32_lock_namespace_wa_diverso_da_lock_instagram():
    from app.browser.context_manager import _get_account_lock

    ig_lock = _get_account_lock("x")
    wa_lock = _get_account_lock("wa_x")
    assert ig_lock is not wa_lock


# ---------------------------------------------------------------------------
# Adversarial E. Concorrenza vera -- 24, 25, 26
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_adv24_due_assisted_login_stesso_numero_mai_due_browser_aperti(wa_number_factory, monkeypatch):
    """Il test piu' importante del batch: il lock per-numero deve impedire
    due launch_persistent_context sovrapposti sullo stesso profilo. Se questo
    fallisse, aprire due browser sullo stesso user-data-dir corromperebbe il
    profilo e provocherebbe il re-scan del QR."""
    number_id = await wa_number_factory()
    probe = _LaunchProbe(launch_delay=0.06)
    _patch_launcher(monkeypatch, probe)
    _patch_wa_page(monkeypatch, segnali=["qr_required"], delay=0.02)

    await asyncio.gather(
        assisted_login(number_id, timeout_s=0),
        assisted_login(number_id, timeout_s=0),
    )

    assert probe.max_concurrent == 1
    assert probe.launch_count == 2  # entrambi hanno aperto, ma mai insieme
    assert probe.close_count == 2


@pytest.mark.asyncio
async def test_adv24b_pulizia_lock_file_non_cancella_un_profilo_in_uso(wa_number_factory, monkeypatch):
    """CRITICAL della review whole-branch 28/07, mancato dai batch 1-4: mkdir
    + pulizia dei lock-file di Chromium (SingletonLock/Cookie/Socket) girava
    FUORI dal lock. Se un browser e' gia' vivo su questo profilo, il suo
    SingletonLock e' VERO (non stale) -- una seconda chiamata concorrente che
    lo pulisce senza aspettare il lock lo cancella MENTRE il primo browser lo
    sta usando, la stessa corruzione del profilo che il lock esiste per
    impedire.

    adv24/25/26 misurano solo le sovrapposizioni di launch_persistent_context
    sul launcher finto, che non scrive mai lock-file veri: non potevano
    vedere questo difetto. Qui il finto scrive un SingletonLock VERO su disco
    quando "apre" e lo rimuove quando "chiude" (real_lock_file), cosi' la
    pulizia REALE di _open_wa_browser (quella che tocca il filesystem, non
    mockata) ha qualcosa di vero su cui agire.

    Con la pulizia FUORI dal lock (bug): la seconda chiamata puo' pulire
    mentre la prima e' nella sua sezione critica -> il file della prima
    sparisce prima che la prima abbia finito di usarlo.
    Con la pulizia DENTRO il lock (fix): la seconda chiamata non puo' nemmeno
    iniziare la sua pulizia finche' la prima non ha rilasciato il lock (cioe'
    finche' non ha gia' chiuso il context e rimosso da sola il proprio file)
    -> il file di ciascuna chiamata esiste sempre quando quella chiamata lo
    usa (session_state, dentro il context)."""
    number_id = await wa_number_factory()
    lock_path = profile_dir_for(number_id) / "SingletonLock"
    probe = _LaunchProbe(launch_delay=0.06, real_lock_file=lock_path)
    _patch_launcher(monkeypatch, probe)

    file_esisteva_durante_uso: list[bool] = []

    async def _osserva_file_durante_uso(_chiamata):
        file_esisteva_durante_uso.append(lock_path.exists())

    _patch_wa_page(monkeypatch, segnali=["qr_required"], delay=0.02,
                    on_call=_osserva_file_durante_uso)

    await asyncio.gather(
        assisted_login(number_id, timeout_s=0),
        assisted_login(number_id, timeout_s=0),
    )

    assert len(file_esisteva_durante_uso) == 2
    assert all(file_esisteva_durante_uso), (
        "il lock-file di una chiamata e' sparito mentre quella chiamata lo "
        "stava ancora usando -- la pulizia dell'altra e' girata fuori dal lock"
    )


@pytest.mark.asyncio
async def test_adv25_check_session_e_assisted_login_stesso_numero_serializzati(wa_number_factory, monkeypatch):
    number_id = await wa_number_factory()
    probe = _LaunchProbe(launch_delay=0.06)
    _patch_launcher(monkeypatch, probe)
    _patch_wa_page(monkeypatch, segnali=["qr_required"], delay=0.02)

    await asyncio.gather(
        check_session(number_id),
        assisted_login(number_id, timeout_s=0),
    )

    assert probe.max_concurrent == 1


@pytest.mark.asyncio
async def test_adv26_due_login_numeri_diversi_non_serializzati(wa_number_factory, monkeypatch):
    """Corollario del 24: il lock e' PER-NUMERO. Se fosse globale, un cliente
    lento bloccherebbe gli altri -- questo test fallisce se qualcuno rende il
    lock condiviso fra numeri diversi."""
    n1 = await wa_number_factory()
    n2 = await wa_number_factory()
    probe = _LaunchProbe(launch_delay=0.08)
    _patch_launcher(monkeypatch, probe)
    _patch_wa_page(monkeypatch, segnali=["qr_required"], delay=0.02)

    await asyncio.gather(
        assisted_login(n1, timeout_s=0),
        assisted_login(n2, timeout_s=0),
    )

    assert probe.max_concurrent == 2


# ---------------------------------------------------------------------------
# Adversarial G. Macchina a stati -- 33-37
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("segnale", ["interstitial", "", None, "LOGGED_IN", "logged_in "])
def test_adv33_segnali_sconosciuti_non_diventano_active(segnale):
    assert stato_da_segnale(segnale) == WaNumberStatus.disconnected


@pytest.mark.asyncio
async def test_adv34_check_session_su_numero_retired_non_torna_active(wa_number_factory, monkeypatch):
    """Decisione del team lead 28/07: retired e' messo da un operatore/dalla
    piattaforma, non e' deducibile dal DOM. Un health-check che vede
    'logged_in' dice 'la sessione browser e' viva', non 'questo numero e' di
    nuovo operativo' -- lo status NON deve muoversi, ma la diagnostica
    (session_checked_at, browser_profile) resta valida e aggiornata."""
    number_id = await wa_number_factory(status=WaNumberStatus.retired)
    probe = _LaunchProbe(launch_delay=0.0)
    _patch_launcher(monkeypatch, probe)
    _patch_wa_page(monkeypatch, segnali=["logged_in"])

    await check_session(number_id)

    numero = await _reload(number_id)
    assert numero.status == WaNumberStatus.retired
    # La resurrezione e' bloccata, ma il check ha comunque fatto il suo
    # lavoro di diagnostica: non deve sembrare che check_session sia stato
    # no-op o abbia fallito silenziosamente.
    assert numero.session_checked_at is not None
    assert numero.browser_profile == str(profile_dir_for(number_id))


@pytest.mark.asyncio
async def test_adv34b_assisted_login_su_numero_suspended_non_torna_active(wa_number_factory, monkeypatch):
    """Gemello del 34 per assisted_login e per lo stato 'suspended': stessa
    regola, stesso motivo -- un login assistito vede 'logged_in' (la
    sessione browser e' viva) ma questo non e' un'azione esplicita
    dell'operatore per far ripartire il numero."""
    number_id = await wa_number_factory(status=WaNumberStatus.suspended)
    probe = _LaunchProbe(launch_delay=0.0)
    _patch_launcher(monkeypatch, probe)
    _patch_wa_page(monkeypatch, segnali=["logged_in"])

    await assisted_login(number_id, timeout_s=0)

    numero = await _reload(number_id)
    assert numero.status == WaNumberStatus.suspended
    assert numero.session_checked_at is not None
    assert numero.browser_profile == str(profile_dir_for(number_id))


@pytest.mark.asyncio
async def test_adv34c_transizioni_normali_non_bloccate_dalla_guardia(wa_number_factory, monkeypatch):
    """La guardia contro la resurrezione automatica riguarda SOLO
    retired/suspended: un numero in qr_required che vede 'logged_in' deve
    ancora diventare active normalmente -- altrimenti la guardia sarebbe
    troppo larga e romperebbe il flusso normale del canale."""
    number_id = await wa_number_factory(status=WaNumberStatus.qr_required)
    probe = _LaunchProbe(launch_delay=0.0)
    _patch_launcher(monkeypatch, probe)
    _patch_wa_page(monkeypatch, segnali=["logged_in"])

    stato = await check_session(number_id)

    assert stato == WaNumberStatus.active
    numero = await _reload(number_id)
    assert numero.status == WaNumberStatus.active


@pytest.mark.asyncio
async def test_adv35_doppio_assisted_login_sequenziale_idempotente(wa_number_factory, monkeypatch):
    number_id = await wa_number_factory()
    probe = _LaunchProbe(launch_delay=0.0)
    _patch_launcher(monkeypatch, probe)
    _patch_wa_page(monkeypatch, segnali=["logged_in"])

    s1 = await assisted_login(number_id, timeout_s=0)
    profilo1 = (await _reload(number_id)).browser_profile
    s2 = await assisted_login(number_id, timeout_s=0)
    profilo2 = (await _reload(number_id)).browser_profile

    assert s1 == s2 == WaNumberStatus.active
    assert profilo1 == profilo2 == str(profile_dir_for(number_id))


@pytest.mark.asyncio
async def test_adv36_check_session_number_id_inesistente_valueerror_prima_del_browser(monkeypatch):
    probe = _LaunchProbe(launch_delay=0.0)
    _patch_launcher(monkeypatch, probe)

    id_fasullo = "id-che-non-esiste-" + str(uuid.uuid4())
    with pytest.raises(ValueError, match="non trovato"):
        await check_session(id_fasullo)

    assert probe.launch_count == 0  # nessun Chromium lanciato per niente


@pytest.mark.parametrize("number_id", [
    "../../etc", "..\\..\\Windows", "../../../x", "..", "a/b", "a\\b", "a/../b",
])
def test_adv37_profile_dir_for_rifiuta_path_traversal(number_id):
    with pytest.raises(ValueError):
        profile_dir_for(number_id)


def test_adv37b_profile_dir_for_id_legittimo_resta_dentro_browser_profiles_dir():
    number_id = str(uuid.uuid4())
    p = profile_dir_for(number_id)
    base = Path(settings.browser_profiles_dir).resolve()
    assert base in p.resolve().parents


# ---------------------------------------------------------------------------
# Adversarial H. Proxy e launcher -- 38-40
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("proxy_malformato", ["http://", "non-un-url", "socks5://user@", "   "])
async def test_adv38_proxy_malformato_valueerror_prima_del_browser(monkeypatch, numero_id_usa_e_getta, proxy_malformato):
    probe = _LaunchProbe(launch_delay=0.0)
    _patch_launcher(monkeypatch, probe)

    with pytest.raises(ValueError, match=re.escape(numero_id_usa_e_getta)):
        async with _open_wa_browser(numero_id_usa_e_getta, headless=True, proxy_url=proxy_malformato):
            pass

    assert probe.launch_count == 0


def test_adv38c_url_di_esempio_del_team_lead_e_valido_non_malformato():
    """Nota di trasparenza, non un'evasione: l'URL suggerito per 38b
    ('http://utente:segretissima@proxy.example:8080/rotto') ha
    hostname='proxy.example' e port=8080 -- entrambi presenti, quindi
    parse_proxy_url lo accetta come proxy VALIDO (la sua validazione guarda
    solo se host/porta mancano, non la sintassi del resto). _open_wa_browser
    non solleva affatto su questo input: non e' malformato per il codice
    attuale, nonostante il suffisso '/rotto'. E' una proprieta' strutturale
    di parse_proxy_url: un input con ENTRAMBI host e porta presenti non e'
    MAI malformato per come e' scritta oggi -- un caso 'malformato con host
    e porta insieme' e' irraggiungibile senza cambiare quella validazione
    (in app/browser/context_manager.py, condivisa col canale Instagram:
    fuori perimetro di questo batch). test_adv38b copre percio' host e
    porta SEPARATAMENTE, coi due modi in cui parse_proxy_url dichiara
    malformato un proxy (uno dei due assente)."""
    from app.browser.context_manager import parse_proxy_url

    url = "http://utente:segretissima@proxy.example:8080/rotto"
    assert parse_proxy_url(url) is not None


@pytest.mark.asyncio
async def test_adv38b_proxy_malformato_con_credenziali_non_le_espone_nel_messaggio(monkeypatch, numero_id_usa_e_getta):
    """Un proxy malformato che porta anche credenziali non deve scriverle in
    chiaro nel ValueError: e' proprio il caso malformato quello in cui
    qualcuno guardera' il log. _mask_proxy_url tiene schema/host/porta
    (utili per capire QUALE proxy e' rotto) e oscura user:pass con '***@'.

    Tre varianti, coprendo i due modi in cui parse_proxy_url dichiara un
    proxy malformato (vedi test_adv38c per il perche' non esiste un caso
    con host E porta insieme):
    - porta assente, host presente -> il messaggio mostra l'host, non le
      credenziali;
    - host assente, porta presente -> il messaggio mostra la porta, non le
      credenziali;
    - nessuna credenziale -> il mascheramento non deve inventarsi un '***@'
      dove non c'e' nulla da nascondere (il messaggio resta pulito anche
      nel caso normale, non solo in quello con segreti)."""
    probe = _LaunchProbe(launch_delay=0.0)
    _patch_launcher(monkeypatch, probe)

    casi = [
        # (proxy_url, atteso_nel_messaggio, vietato_nel_messaggio)
        ("http://utente:segretissima@proxy.example", "proxy.example", "segretissima"),
        ("http://utente:segretissima@:8080", "8080", "segretissima"),
    ]
    for proxy_malformato, atteso, vietato in casi:
        with pytest.raises(ValueError) as exc_info:
            async with _open_wa_browser(numero_id_usa_e_getta, headless=True,
                                         proxy_url=proxy_malformato):
                pass
        messaggio = str(exc_info.value)
        assert vietato not in messaggio
        assert "utente" not in messaggio
        assert atteso in messaggio
        assert numero_id_usa_e_getta in messaggio  # il problema resta nominato

    # Variante senza credenziali: il mascheramento non deve rovinare il
    # messaggio nel caso normale (nessun '***@' fabbricato dal nulla).
    with pytest.raises(ValueError) as exc_info:
        async with _open_wa_browser(numero_id_usa_e_getta, headless=True,
                                     proxy_url="http://proxy.example"):
            pass
    messaggio_senza_credenziali = str(exc_info.value)
    assert "proxy.example" in messaggio_senza_credenziali
    assert "***" not in messaggio_senza_credenziali

    assert probe.launch_count == 0


@pytest.mark.asyncio
async def test_adv39_proxy_none_logga_warning_esplicito_e_procede(monkeypatch, numero_id_usa_e_getta):
    probe = _LaunchProbe(launch_delay=0.0)
    _patch_launcher(monkeypatch, probe)

    messages: list[str] = []
    sink_id = loguru_logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        async with _open_wa_browser(numero_id_usa_e_getta, headless=True, proxy_url=None):
            pass
    finally:
        loguru_logger.remove(sink_id)

    assert probe.launch_count == 1
    assert any("SENZA proxy" in m for m in messages)


@pytest.mark.asyncio
async def test_adv40_launch_solleva_propaga_e_non_scrive_stato(wa_number_factory, monkeypatch):
    number_id = await wa_number_factory(status=WaNumberStatus.active)
    probe = _LaunchProbe(launch_delay=0.0)
    probe.should_raise = RuntimeError("TargetClosedError simulato")
    _patch_launcher(monkeypatch, probe)
    _patch_wa_page(monkeypatch, segnali=["logged_in"])

    with pytest.raises(RuntimeError):
        await check_session(number_id)

    numero = await _reload(number_id)
    assert numero.status == WaNumberStatus.active  # invariato, MAI qr_required per questo
    assert numero.session_checked_at is None


@pytest.mark.asyncio
async def test_adv40b_patchright_non_installato_import_error_propaga(wa_number_factory, monkeypatch):
    number_id = await wa_number_factory(status=WaNumberStatus.active)
    # Simula patchright assente: sys.modules[name] = None fa risollevare
    # ImportError a qualunque `from patchright.async_api import ...`.
    # monkeypatch.setitem ripristina il valore vero a fine test.
    monkeypatch.setitem(sys.modules, "patchright.async_api", None)

    with pytest.raises(ImportError):
        await check_session(number_id)

    numero = await _reload(number_id)
    assert numero.status == WaNumberStatus.active


# ---------------------------------------------------------------------------
# Adversarial I. Persistenza -- 41-43
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_adv41_wanumber_cancellato_a_meta_flusso_valueerror_leggibile(wa_number_factory, monkeypatch):
    number_id = await wa_number_factory()
    probe = _LaunchProbe(launch_delay=0.0)
    _patch_launcher(monkeypatch, probe)

    async def _cancella_durante_il_giro(_chiamata):
        from sqlalchemy import delete

        from app.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            await db.execute(delete(WaNumber).where(WaNumber.id == number_id))
            await db.commit()

    _patch_wa_page(monkeypatch, segnali=["logged_in"], on_call=_cancella_durante_il_giro)

    with pytest.raises(ValueError, match=re.escape(number_id)):
        await check_session(number_id)


@pytest.mark.asyncio
async def test_adv42_commit_fallisce_in_persist_status_non_scrive_a_meta(wa_number_factory, monkeypatch):
    number_id = await wa_number_factory(status=WaNumberStatus.pending_qr)
    probe = _LaunchProbe(launch_delay=0.0)
    _patch_launcher(monkeypatch, probe)
    _patch_wa_page(monkeypatch, segnali=["logged_in"])

    from sqlalchemy.ext.asyncio import AsyncSession

    async def _commit_fallisce(self, *a, **kw):
        raise RuntimeError("commit fallito (simulato)")

    monkeypatch.setattr(AsyncSession, "commit", _commit_fallisce)

    with pytest.raises(RuntimeError):
        await check_session(number_id)

    numero = await _reload(number_id)
    # Nessuna scrittura a meta': lo stato resta quello di partenza, non 'active'
    # ne' un valore intermedio.
    assert numero.status == WaNumberStatus.pending_qr
    assert numero.session_checked_at is None


@pytest.mark.asyncio
async def test_adv43_eccezione_nel_context_chiude_comunque_e_libera_il_lock(wa_number_factory, monkeypatch):
    number_id = await wa_number_factory()
    probe = _LaunchProbe(launch_delay=0.0)
    _patch_launcher(monkeypatch, probe)

    class _DomEsploso(Exception):
        pass

    async def _esplodi(_chiamata):
        raise _DomEsploso("dom esploso durante session_state")

    _patch_wa_page(monkeypatch, segnali=["logged_in"], on_call=_esplodi)

    with pytest.raises(_DomEsploso):
        await check_session(number_id)

    assert probe.close_count == 1  # il finally ha chiuso il context comunque
    lock = _get_wa_lock(number_id)
    assert not lock.locked()  # il lock non resta trattenuto per sempre
