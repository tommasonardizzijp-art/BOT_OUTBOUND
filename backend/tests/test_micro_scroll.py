"""Micro-scroll: probabilistico e non-bloccante; scroll solo entro il ratio.
Sui profili pubblici puo' anche aprire un post (bio_browser_open_post_ratio);
sui privati resta un tocco breve e non apre mai nulla (non c'e' una griglia
di post da guardare). Pubblico con griglia VUOTA (task B.2): niente scroll
lungo, niente apertura post -- vedi test_public_profile_* sotto.

`post_count` (review B.2): segnale PRIMARIO deterministico quando il payload
gia' scaricato lo porta (edge_owner_to_timeline_media.count) -- zero attesa
DOM, `wait_for` non deve nemmeno essere chiamato. Quando e' `None` (il caso
comune con la sorgente GraphQL passiva, che non porta il conteggio) si
ripiega sul `wait_for`: i test SENZA `post_count` esplicito (sopra, gia'
scritti) esercitano gia' quel ramo -- non duplicati qui."""
import random
import pytest

from app.services import browser_bio


class _RawPage:
    def __init__(self, post_count=1):
        self.scrolled = 0
        self.post_opened = False
        self.went_back = False
        self.post_count = post_count  # 0 = griglia vuota
        self.wait_for_calls = 0       # quante volte il ripiego DOM e' stato invocato

    async def evaluate(self, *a, **k):
        self.scrolled += 1

    def locator(self, selector):
        return _Locator(self)

    async def go_back(self, *a, **k):
        self.went_back = True


class _Locator:
    def __init__(self, raw):
        self._raw = raw

    @property
    def first(self):
        return self

    async def count(self):
        return self._raw.post_count

    async def wait_for(self, state=None, timeout=None):
        # Mock del polling reale di Playwright: risolve subito se c'e' almeno
        # un post, altrimenti si comporta come un timeout scaduto (nessun
        # sleep reale nel mock -- il codice sotto test non dipende dal tempo,
        # solo dall'esito wait_for OK/eccezione).
        self._raw.wait_for_calls += 1
        if self._raw.post_count <= 0:
            raise TimeoutError("nessun post nella griglia (mock)")

    async def click(self, timeout=None):
        self._raw.post_opened = True


class _Page:
    def __init__(self, raw): self._raw = raw
    async def _get_page(self): return self._raw


class _Session:
    def __init__(self, raw=None): self.page = _Page(raw or _RawPage())


@pytest.mark.asyncio
async def test_scrolls_when_below_ratio(monkeypatch):
    monkeypatch.setattr(browser_bio.settings, "bio_browser_scroll_ratio", 1.0)
    monkeypatch.setattr(browser_bio.asyncio, "sleep", lambda *_: _noop())
    s = _Session()
    did = await browser_bio.maybe_micro_scroll(s, rng=random.Random(1))
    assert did is True


@pytest.mark.asyncio
async def test_skips_when_ratio_zero(monkeypatch):
    monkeypatch.setattr(browser_bio.settings, "bio_browser_scroll_ratio", 0.0)
    s = _Session()
    did = await browser_bio.maybe_micro_scroll(s, rng=random.Random(1))
    assert did is False


@pytest.mark.asyncio
async def test_public_profile_can_open_post(monkeypatch):
    # post_count NON passato (default None) -- esercita il RIPIEGO wait_for,
    # non il segnale primario (vedi test_public_profile_post_count_positive_*
    # sotto per il segnale primario con lo stesso esito comportamentale).
    monkeypatch.setattr(browser_bio.settings, "bio_browser_scroll_ratio", 1.0)
    monkeypatch.setattr(browser_bio.settings, "bio_browser_open_post_ratio", 1.0)
    monkeypatch.setattr(browser_bio.asyncio, "sleep", lambda *_: _noop())
    raw = _RawPage(post_count=1)  # griglia con post
    s = _Session(raw)
    did = await browser_bio.maybe_micro_scroll(s, is_private=False, rng=random.Random(1))
    assert did is True
    assert raw.post_opened is True
    # Scroll LUNGO come oggi: multipli scrollBy (steps = max(1, dur/1.5)), non
    # la singola occhiata breve del ramo griglia-vuota (task B.2).
    assert raw.scrolled > 1
    assert raw.wait_for_calls == 1     # ripiego DOM usato (post_count assente)


@pytest.mark.asyncio
async def test_public_profile_empty_grid_skips_long_scroll_and_post(monkeypatch):
    # Task B.2: pubblico ma SENZA post -- una persona vera se ne va subito,
    # non scorre 6-10s sul nulla. Niente scroll lungo, niente tentativo di
    # aprire un post (non c'e' niente da aprire). post_count NON passato:
    # ripiego wait_for (vedi test_public_profile_post_count_zero_* sotto per
    # lo stesso esito via segnale primario, senza attesa DOM).
    monkeypatch.setattr(browser_bio.settings, "bio_browser_scroll_ratio", 1.0)
    monkeypatch.setattr(browser_bio.settings, "bio_browser_open_post_ratio", 1.0)
    monkeypatch.setattr(browser_bio.asyncio, "sleep", lambda *_: _noop())
    raw = _RawPage(post_count=0)  # griglia vuota
    s = _Session(raw)
    did = await browser_bio.maybe_micro_scroll(s, is_private=False, rng=random.Random(1))
    assert did is True                 # ha comunque "guardato" (occhiata breve)
    assert raw.scrolled == 0           # nessuno scrollBy: niente scroll lungo
    assert raw.post_opened is False    # nessun tentativo di apertura post
    assert raw.wait_for_calls == 1     # ripiego DOM usato (post_count assente)


@pytest.mark.asyncio
async def test_public_profile_post_count_zero_skips_dom_wait(monkeypatch):
    # Review B.2: quando il payload GIA' scaricato porta post_count=0, la
    # griglia vuota e' NOTA in anticipo -- non serve nessuna attesa sul DOM.
    # `_RawPage(post_count=5)` (diverso da 0!) dimostra che la decisione segue
    # il PARAMETRO post_count passato esplicitamente, non il DOM sottostante:
    # se il codice ignorasse post_count e ricadesse comunque sul wait_for
    # mockato, vedrebbe post_count=5 sul raw page e sbaglierebbe ramo.
    monkeypatch.setattr(browser_bio.settings, "bio_browser_scroll_ratio", 1.0)
    monkeypatch.setattr(browser_bio.settings, "bio_browser_open_post_ratio", 1.0)
    monkeypatch.setattr(browser_bio.asyncio, "sleep", lambda *_: _noop())
    raw = _RawPage(post_count=5)  # DOM avrebbe post: la decisione NON deve guardarlo
    s = _Session(raw)
    did = await browser_bio.maybe_micro_scroll(s, is_private=False, post_count=0, rng=random.Random(1))
    assert did is True
    assert raw.scrolled == 0
    assert raw.post_opened is False
    assert raw.wait_for_calls == 0     # nessuna attesa DOM: segnale primario deterministico


@pytest.mark.asyncio
async def test_public_profile_post_count_positive_skips_dom_wait(monkeypatch):
    # Simmetrico: post_count>0 esplicito -- scroll lungo SENZA passare dal
    # wait_for, anche se il DOM mockato (post_count=0) direbbe "vuoto".
    # open_post_ratio=0.0: isola la decisione griglia-vuota/piena (post_count)
    # dal sotto-check LIVE sul DOM che apre un post specifico (count() proprio
    # locator, invariato da questa review, fuori scope qui).
    monkeypatch.setattr(browser_bio.settings, "bio_browser_scroll_ratio", 1.0)
    monkeypatch.setattr(browser_bio.settings, "bio_browser_open_post_ratio", 0.0)
    monkeypatch.setattr(browser_bio.asyncio, "sleep", lambda *_: _noop())
    raw = _RawPage(post_count=0)  # DOM direbbe vuoto: la decisione NON deve guardarlo
    s = _Session(raw)
    did = await browser_bio.maybe_micro_scroll(s, is_private=False, post_count=12, rng=random.Random(1))
    assert did is True
    assert raw.scrolled > 1
    assert raw.wait_for_calls == 0     # nessuna attesa DOM: segnale primario deterministico


@pytest.mark.asyncio
async def test_private_profile_no_post(monkeypatch):
    # Task B.2: il ramo privato resta INVARIATO -- non guarda mai la griglia
    # (post_count=0 qui sotto, eppure scrolla comunque: sui privati non c'e'
    # nessuna griglia da controllare, il caso "griglia vuota" non lo tocca).
    monkeypatch.setattr(browser_bio.settings, "bio_browser_scroll_ratio", 1.0)
    # anche con ratio 1.0 il ramo privato non deve MAI valutare l'apertura post
    monkeypatch.setattr(browser_bio.settings, "bio_browser_open_post_ratio", 1.0)
    monkeypatch.setattr(browser_bio.asyncio, "sleep", lambda *_: _noop())
    raw = _RawPage(post_count=0)
    s = _Session(raw)
    did = await browser_bio.maybe_micro_scroll(s, is_private=True, rng=random.Random(1))
    assert did is True
    assert raw.post_opened is False
    assert raw.scrolled > 0    # scroll breve dell'header, indifferente alla griglia


async def _noop():
    return None
