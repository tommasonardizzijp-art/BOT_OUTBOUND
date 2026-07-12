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


# ── _verify_dm_thread: il thread DM aperto appartiene davvero al destinatario? ──
# Guardia anti-misinstradamento: quando il click su "Messaggio" atterra sull'inbox
# con un ALTRO thread in cima (bug reale: 26 DM finiti nel thread di giovanni), il
# link al profilo del destinatario NON e' presente -> non scrivere alla persona sbagliata.

def test_verify_thread_true_quando_link_profilo_del_target_presente():
    # Thread corretto aperto: header ha a[href="/target/"]
    page = _FakePage(
        url="https://www.instagram.com/direct/t/111/",
        visible_selectors={'a[href="/target/"]'},
    )
    assert asyncio.run(InstagramPage(None)._verify_dm_thread(page, "target"))


def test_verify_thread_false_quando_thread_di_altra_persona_aperto():
    # Misinstradamento: e' aperto il thread di giovanni1927, non di target.
    page = _FakePage(
        url="https://www.instagram.com/direct/t/999/",
        visible_selectors={'a[href="/giovanni1927/"]'},
    )
    assert not asyncio.run(InstagramPage(None)._verify_dm_thread(page, "target"))


def test_verify_thread_false_su_inbox_senza_link_profilo():
    # Inbox generica: nessun link al profilo del target -> non verificato.
    page = _FakePage(url="https://www.instagram.com/direct/inbox/")
    assert not asyncio.run(InstagramPage(None)._verify_dm_thread(page, "target"))


def test_verify_thread_username_case_insensitive_e_at():
    # IG usa href lowercase; il metodo normalizza username (@ e maiuscole).
    page = _FakePage(
        url="https://www.instagram.com/direct/t/111/",
        visible_selectors={'a[href="/target/"]'},
    )
    assert asyncio.run(InstagramPage(None)._verify_dm_thread(page, "@Target"))


# ── _dismiss_ig_modals: mai cliccare elementi del profilo scambiati per popup ──
# Bug reale: `[role="button"]:has-text("OK")` (substring, case-insensitive) matchava
# il cerchio highlight "New Bo(OK)" -> il bot apriva il viewer storie e l'invio
# falliva dentro il viewer. I selettori devono essere ESATTI e scoped al dialog.

class _ClickTrackingLocator:
    def __init__(self, page, selector: str) -> None:
        self._page = page
        self._selector = selector

    @property
    def first(self):
        return self

    async def count(self) -> int:
        return 1 if self._selector in self._page.present_selectors else 0

    async def is_visible(self) -> bool:
        return self._selector in self._page.present_selectors

    async def click(self) -> None:
        self._page.clicked.append(self._selector)


class _ModalPage:
    def __init__(self, present_selectors: set[str]) -> None:
        self.url = "https://www.instagram.com/target/"
        self.present_selectors = present_selectors
        self.clicked: list[str] = []

    def locator(self, selector: str) -> _ClickTrackingLocator:
        return _ClickTrackingLocator(self, selector)


def test_dismiss_modals_non_clicca_highlight_con_ok_nel_titolo(monkeypatch):
    _no_sleep(monkeypatch)
    # Profilo con highlight "New Book": i VECCHI selettori broad matchavano.
    # Nessun dialog aperto -> il metodo non deve cliccare NULLA.
    page = _ModalPage(present_selectors={
        'button:has-text("OK")',            # match del vecchio selettore broad
        '[role="button"]:has-text("OK")',   # idem
    })
    asyncio.run(InstagramPage(None)._dismiss_ig_modals(page, "target"))
    assert page.clicked == []


def test_dismiss_modals_clicca_ok_dentro_dialog(monkeypatch):
    _no_sleep(monkeypatch)
    page = _ModalPage(present_selectors={'div[role="dialog"] button:text-is("OK")'})
    asyncio.run(InstagramPage(None)._dismiss_ig_modals(page, "target"))
    assert page.clicked == ['div[role="dialog"] button:text-is("OK")']


def test_dismiss_modals_supporta_ui_italiana(monkeypatch):
    _no_sleep(monkeypatch)
    # Popup "sleep mode" con UI italiana: "Non ora" dentro il dialog.
    page = _ModalPage(present_selectors={'div[role="dialog"] button:text-is("Non ora")'})
    asyncio.run(InstagramPage(None)._dismiss_ig_modals(page, "target"))
    assert page.clicked == ['div[role="dialog"] button:text-is("Non ora")']
