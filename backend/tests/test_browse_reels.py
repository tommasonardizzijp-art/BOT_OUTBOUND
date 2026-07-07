"""browse_reels: pausa ATTIVA sui Reel (rimpiazza lo standing-still). Naviga
verso /reels/, poi guarda N reel (una wheel decisa a schermo intero = reel
successivo), e non deve mai sollevare — un errore di navigazione ricade su un
semplice sleep proporzionato alla sessione mancata."""
import pytest

from app.browser import instagram_page as ip_module
from app.browser.instagram_page import InstagramPage


class _FakeLocator:
    def __init__(self, count):
        self._count = count

    @property
    def first(self):
        return self

    async def count(self):
        return self._count

    async def click(self, timeout=None):
        pass

    async def bounding_box(self):
        return {"x": 10, "y": 10, "width": 20, "height": 20}


class _FakeMouse:
    def __init__(self):
        self.wheel_calls = 0

    async def wheel(self, x, y):
        self.wheel_calls += 1

    async def move(self, x, y, steps=None):
        pass

    async def click(self, x, y):
        pass


class _FakeKeyboard:
    def __init__(self):
        self.presses = []

    async def press(self, key):
        self.presses.append(key)


class _FakePage:
    def __init__(self, nav_link_count=0, raise_on_goto=False):
        self.mouse = _FakeMouse()
        self.keyboard = _FakeKeyboard()
        self.viewport_size = {"width": 1280, "height": 900}
        self._nav_link_count = nav_link_count
        self.raise_on_goto = raise_on_goto
        self.goto_calls = []

    def is_closed(self):
        return False

    def locator(self, selector):
        return _FakeLocator(self._nav_link_count)

    async def goto(self, url, wait_until=None):
        self.goto_calls.append(url)
        if self.raise_on_goto:
            raise RuntimeError("navigation down")


def _make_ig(page):
    ig = InstagramPage(context=None)
    ig._page = page
    return ig


@pytest.mark.asyncio
async def test_browse_reels_navigates_and_scrolls(monkeypatch):
    monkeypatch.setattr(ip_module.random, "uniform", lambda a, b: 0.01)
    monkeypatch.setattr(ip_module.random, "random", lambda: 0.99)  # niente like raro
    page = _FakePage(nav_link_count=0)  # niente link nav -> goto di fallback
    ig = _make_ig(page)

    # 3 reel richiesti -> esattamente 3 avanzamenti (una wheel per reel).
    await ig.browse_reels(3, dwell_min_s=0.0, dwell_max_s=0.0)

    assert any("/reels/" in u for u in page.goto_calls)
    assert page.mouse.wheel_calls == 3


@pytest.mark.asyncio
async def test_browse_reels_zero_reels_navigates_but_no_scroll(monkeypatch):
    monkeypatch.setattr(ip_module.random, "uniform", lambda a, b: 0.01)
    monkeypatch.setattr(ip_module.random, "random", lambda: 0.99)
    page = _FakePage(nav_link_count=0)
    ig = _make_ig(page)

    # n_reels=0 -> naviga sui reel ma non scrolla (glance e via).
    await ig.browse_reels(0, dwell_min_s=0.0, dwell_max_s=0.0)

    assert any("/reels/" in u for u in page.goto_calls)
    assert page.mouse.wheel_calls == 0


@pytest.mark.asyncio
async def test_browse_reels_navigation_error_falls_back_no_raise(monkeypatch):
    monkeypatch.setattr(ip_module.random, "uniform", lambda a, b: 0.01)
    monkeypatch.setattr(ip_module.random, "random", lambda: 0.99)
    page = _FakePage(nav_link_count=0, raise_on_goto=True)
    ig = _make_ig(page)

    # non deve sollevare: l'eccezione in navigazione ricade su un asyncio.sleep
    await ig.browse_reels(3, dwell_min_s=0.0, dwell_max_s=0.0)


@pytest.mark.asyncio
async def test_browse_reels_cannot_get_page_falls_back(monkeypatch):
    ig = InstagramPage(context=None)

    async def _boom():
        raise RuntimeError("no context")
    monkeypatch.setattr(ig, "_get_page", _boom)

    slept = {"v": None}

    async def fake_sleep(seconds):
        slept["v"] = seconds
    monkeypatch.setattr(ip_module.asyncio, "sleep", fake_sleep)

    # fallback = min(120, max(1, n_reels) * uniform(dwell)) con dwell fisso 2s:
    #   5 reel * 2.0 = 10.0s.
    await ig.browse_reels(5, dwell_min_s=2.0, dwell_max_s=2.0)
    assert slept["v"] == 10.0
