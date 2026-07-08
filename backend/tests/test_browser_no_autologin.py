"""Fix A: lo scraping NON deve MAI fare login automatico (ban risk). Con
allow_login=False, una sessione scaduta (redirect a /accounts/login) solleva
AccountSessionExpiredError invece di re-inserire le credenziali."""
import pytest

from app.browser import instagram_page as ip_module
from app.browser.instagram_page import InstagramPage
from app.utils.exceptions import AccountSessionExpiredError


class _LoginPage:
    """Page fake ferma sulla pagina di login."""
    url = "https://www.instagram.com/accounts/login/"

    def is_closed(self):
        return False

    async def goto(self, url, wait_until=None):
        return None

    def locator(self, selector):  # non deve essere raggiunto quando siamo su login
        raise AssertionError("non deve valutare il locator quando e' sulla login page")


@pytest.mark.asyncio
async def test_no_autologin_raises_session_expired(monkeypatch):
    async def _fast(*a, **k):
        return None
    monkeypatch.setattr(ip_module.asyncio, "sleep", _fast)

    ig = InstagramPage(context=None)
    ig._page = _LoginPage()

    called = {"login": False}

    async def _boom_login(*a, **k):
        called["login"] = True
    monkeypatch.setattr(ig, "_do_login", _boom_login)

    with pytest.raises(AccountSessionExpiredError):
        await ig.ensure_logged_in("acc-1", allow_login=False)
    assert called["login"] is False  # login automatico MAI tentato


@pytest.mark.asyncio
async def test_login_allowed_when_permitted(monkeypatch):
    async def _fast(*a, **k):
        return None
    monkeypatch.setattr(ip_module.asyncio, "sleep", _fast)

    ig = InstagramPage(context=None)
    ig._page = _LoginPage()

    called = {"login": False}

    async def _login(account_id, page):
        called["login"] = True
    monkeypatch.setattr(ig, "_do_login", _login)

    await ig.ensure_logged_in("acc-1", allow_login=True)  # default: puo' loggare
    assert called["login"] is True
