"""C.2: il lock Redis e' collegato nel chokepoint (`get_browser_context` e
`BrowserSession`), non solo definito a fianco. Verifica che un secondo
tentativo concorrente sullo STESSO account_id venga rifiutato PRIMA di
lanciare un secondo Chromium — la garanzia che i test unitari di
`profile_lock` da soli non possono dare (testano il lock isolato, non che
sia davvero nel percorso che apre il browser).

Stile del probe (launch counter + delay reale) ricalcato da test_wa_session.py
(_LaunchProbe / _patch_launcher), stesso motivo: senza un `await` vero dentro
il launch finto, asyncio non cederebbe mai il controllo all'altra coroutine e
il test passerebbe per finta anche con un lock rotto."""
import asyncio
import uuid
from types import SimpleNamespace

import fakeredis.aioredis
import pytest

import app.browser.context_manager as context_manager
import app.browser.profile_lock as profile_lock
from app.browser.profile_lock import AccountBrowserBusy


@pytest.fixture
def fake_redis(monkeypatch):
    client = fakeredis.aioredis.FakeRedis()

    async def _fake_pool():
        return client

    monkeypatch.setattr(profile_lock.arq, "create_pool", lambda *_a, **_k: _fake_pool())
    return client


class _LaunchProbe:
    def __init__(self, launch_delay: float = 0.05):
        self.current = 0
        self.max_concurrent = 0
        self.launch_count = 0

    async def launch(self, **kwargs):
        self.launch_count += 1
        self.current += 1
        self.max_concurrent = max(self.max_concurrent, self.current)
        await asyncio.sleep(0.05)  # un await reale: fa girare l'altra coroutine
        return _FakeContext(self)


class _FakeContext:
    def __init__(self, probe: _LaunchProbe):
        self._probe = probe

    async def new_page(self):
        return SimpleNamespace()

    async def add_init_script(self, script):
        pass

    async def close(self):
        self._probe.current -= 1


class _FakePlaywrightCM:
    def __init__(self, probe: _LaunchProbe):
        self._probe = probe

    async def __aenter__(self):
        chromium = SimpleNamespace(launch_persistent_context=self._probe.launch)
        return SimpleNamespace(chromium=chromium)

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _patch_launcher(monkeypatch, probe: _LaunchProbe) -> None:
    import patchright.async_api as pw_api
    monkeypatch.setattr(pw_api, "async_playwright", lambda: _FakePlaywrightCM(probe))


def _disattiva_lock_in_process(monkeypatch) -> None:
    """I due tentativi concorrenti in questi test girano sullo STESSO event
    loop (asyncio.gather in un solo test): li' `_get_account_lock` (il mutex
    in-process, vedi il suo docstring in context_manager.py) funziona
    correttamente e mette B in coda finche' A non rilascia -- il che
    NASCONDE quello che si vuole isolare qui, cioe' se il lock REDIS da solo
    rifiuta un secondo apertura. In produzione il caso che conta e' cross-
    processo (backend vs worker ARQ), dove quel mutex non esiste proprio:
    disattivarlo qui e' la simulazione piu' fedele di quel caso, non un
    trucco per far passare il test. Un Lock nuovo ad ogni chiamata non
    contende mai con se stesso -- equivale a "nessun mutex in-process"."""
    import asyncio as _asyncio
    monkeypatch.setattr(context_manager, "_get_account_lock", lambda account_id: _asyncio.Lock())


@pytest.mark.asyncio
async def test_get_browser_context_secondo_tentativo_concorrente_e_rifiutato(fake_redis, monkeypatch):
    account_id = f"acc-{uuid.uuid4().hex[:8]}"
    probe = _LaunchProbe()
    _patch_launcher(monkeypatch, probe)
    _disattiva_lock_in_process(monkeypatch)

    esiti = []

    async def _apri_a():
        async with context_manager.get_browser_context(account_id, headless=True, proxy_url=None):
            await asyncio.sleep(0.1)
        esiti.append("a-ok")

    async def _apri_b():
        await asyncio.sleep(0.02)  # parte mentre A ha gia' il browser aperto
        try:
            async with context_manager.get_browser_context(account_id, headless=True, proxy_url=None):
                pass
            esiti.append("b-ok")
        except AccountBrowserBusy:
            esiti.append("b-busy")

    await asyncio.gather(_apri_a(), _apri_b())

    assert "b-busy" in esiti
    assert probe.launch_count == 1, "un secondo Chromium e' stato lanciato sullo stesso profilo"
    assert probe.max_concurrent == 1


@pytest.mark.asyncio
async def test_browser_session_secondo_tentativo_concorrente_e_rifiutato(fake_redis, monkeypatch):
    account_id = f"acc-{uuid.uuid4().hex[:8]}"
    probe = _LaunchProbe()
    _patch_launcher(monkeypatch, probe)
    _disattiva_lock_in_process(monkeypatch)

    esiti = []

    async def _apri_a():
        session = context_manager.BrowserSession(account_id, headless=True)
        await session.open()
        await asyncio.sleep(0.1)
        await session.close()
        esiti.append("a-ok")

    async def _apri_b():
        await asyncio.sleep(0.02)
        session = context_manager.BrowserSession(account_id, headless=True)
        try:
            await session.open()
            await session.close()
            esiti.append("b-ok")
        except AccountBrowserBusy:
            esiti.append("b-busy")

    await asyncio.gather(_apri_a(), _apri_b())

    assert "b-busy" in esiti
    assert probe.launch_count == 1, "un secondo Chromium e' stato lanciato sullo stesso profilo"
    assert probe.max_concurrent == 1


@pytest.mark.asyncio
async def test_browser_session_rilascia_il_lock_anche_se_open_fallisce_dopo(fake_redis, monkeypatch):
    """open() acquisisce il lock Redis PRIMA del launch; se il launch stesso
    fallisce (es. Chromium crash), open() richiama close() e ripropaga —
    il lock non deve restare preso."""
    account_id = f"acc-{uuid.uuid4().hex[:8]}"

    class _BrokenProbe(_LaunchProbe):
        async def launch(self, **kwargs):
            raise RuntimeError("chromium crash simulato")

    _patch_launcher(monkeypatch, _BrokenProbe())

    session = context_manager.BrowserSession(account_id, headless=True)
    with pytest.raises(RuntimeError):
        await session.open()

    assert not await fake_redis.exists(f"ig:profile-lock:{account_id}")
    # Un secondo tentativo, dopo il fallimento, deve poter acquisire di nuovo.
    async with profile_lock.held(account_id):
        pass


@pytest.mark.asyncio
async def test_prova_del_nove_senza_lock_redis_il_secondo_launch_partirebbe(fake_redis, monkeypatch):
    """Prova del nove: se il lock Redis non fosse collegato nel chokepoint
    (bypassato qui sostituendo held_with_renew con un passthrough che non fa
    nulla), il secondo BrowserSession.open() concorrente NON verrebbe
    rifiutato — lancerebbe un secondo Chromium sullo stesso profilo, il
    danno esatto che C.2 previene. Dimostra che i due test sopra stanno
    verificando qualcosa di reale, non un artefatto del probe."""
    from contextlib import asynccontextmanager

    account_id = f"acc-{uuid.uuid4().hex[:8]}"
    probe = _LaunchProbe()
    _patch_launcher(monkeypatch, probe)
    _disattiva_lock_in_process(monkeypatch)  # isola l'effetto: solo il lock Redis viene tolto

    @asynccontextmanager
    async def _passthrough(*_a, **_k):
        yield "token-finto"

    # held_with_renew e' importato LOCALMENTE dentro BrowserSession.open()
    # (`from app.browser.profile_lock import held_with_renew`): va patchato
    # sul modulo sorgente, non su context_manager (che non lo tiene come
    # attributo di modulo) — altrimenti il patch e' un no-op silenzioso.
    monkeypatch.setattr(profile_lock, "held_with_renew", _passthrough)

    async def _apri():
        session = context_manager.BrowserSession(account_id, headless=True)
        await session.open()
        await asyncio.sleep(0.1)
        await session.close()

    await asyncio.gather(_apri(), _apri())

    assert probe.max_concurrent == 2, (
        "senza il lock collegato, due Chromium sullo stesso profilo partono in parallelo"
    )
