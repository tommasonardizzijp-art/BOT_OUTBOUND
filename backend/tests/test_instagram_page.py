import asyncio

from app.browser.instagram_page import InstagramPage


class _FakeLocator:
    def __init__(self, count: int, visible: bool = True) -> None:
        self._count = count
        self._visible = visible

    @property
    def first(self):
        return self

    async def count(self) -> int:
        return self._count

    async def is_visible(self) -> bool:
        return self._visible


class _FakePage:
    def __init__(self, *, url: str, visible_selectors: set[str] | None = None) -> None:
        self.url = url
        self._visible_selectors = visible_selectors or set()

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(1 if selector in self._visible_selectors else 0)


def test_stories_viewer_detects_story_url():
    page = _FakePage(url="https://www.instagram.com/stories/example/123/")

    assert asyncio.run(InstagramPage(None)._is_stories_viewer_open(page))


def test_stories_viewer_detects_reply_input_overlay():
    page = _FakePage(
        url="https://www.instagram.com/example/",
        visible_selectors={'input[placeholder*="Reply"]'},
    )

    assert asyncio.run(InstagramPage(None)._is_stories_viewer_open(page))


def test_stories_viewer_ignores_normal_profile():
    page = _FakePage(url="https://www.instagram.com/example/")

    assert not asyncio.run(InstagramPage(None)._is_stories_viewer_open(page))


# ── _dismiss_blocking_dialog: chiude il modale 'Link' prima del click Messaggio ──

class _DialogLoc:
    def __init__(self, page):
        self._page = page

    @property
    def first(self):
        return self

    async def count(self) -> int:
        return 1 if self._page.dialog_open else 0

    async def click(self) -> None:
        self._page.x_clicks += 1
        self._page.dialog_open = False  # la X chiude il dialog


class _Keyboard:
    def __init__(self, page):
        self._page = page

    async def press(self, key: str) -> None:
        self._page.escape_presses += 1
        if key == "Escape" and self._page.escape_closes:
            self._page.dialog_open = False


class _DialogPage:
    def __init__(self, *, url: str, dialog_open: bool, escape_closes: bool = True) -> None:
        self.url = url
        self.dialog_open = dialog_open
        self.escape_closes = escape_closes
        self.escape_presses = 0
        self.x_clicks = 0
        self.keyboard = _Keyboard(self)

    def locator(self, selector: str) -> _DialogLoc:
        # dialog e X close: entrambi presenti finche' dialog_open
        return _DialogLoc(self)


def _no_sleep(monkeypatch):
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop)


def test_dismiss_link_modal_escape_basta(monkeypatch):
    _no_sleep(monkeypatch)
    page = _DialogPage(url="https://www.instagram.com/target/", dialog_open=True, escape_closes=True)
    asyncio.run(InstagramPage(None)._dismiss_blocking_dialog(page, "target"))
    assert page.escape_presses == 1
    assert page.dialog_open is False
    assert page.x_clicks == 0  # Escape ha chiuso, niente fallback X


def test_dismiss_link_modal_fallback_x_se_escape_fallisce(monkeypatch):
    _no_sleep(monkeypatch)
    page = _DialogPage(url="https://www.instagram.com/target/", dialog_open=True, escape_closes=False)
    asyncio.run(InstagramPage(None)._dismiss_blocking_dialog(page, "target"))
    assert page.escape_presses == 1
    assert page.x_clicks == 1
    assert page.dialog_open is False


def test_dismiss_link_modal_noop_nel_thread_direct(monkeypatch):
    _no_sleep(monkeypatch)
    page = _DialogPage(url="https://www.instagram.com/direct/t/123/", dialog_open=True)
    asyncio.run(InstagramPage(None)._dismiss_blocking_dialog(page, "target"))
    assert page.escape_presses == 0      # non tocca il thread DM
    assert page.dialog_open is True


def test_dismiss_link_modal_noop_senza_dialog(monkeypatch):
    _no_sleep(monkeypatch)
    page = _DialogPage(url="https://www.instagram.com/target/", dialog_open=False)
    asyncio.run(InstagramPage(None)._dismiss_blocking_dialog(page, "target"))
    assert page.escape_presses == 0
