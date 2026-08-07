# backend/tests/test_send_dm_block_detect.py
"""send_dm deve fermarsi se dopo il goto siamo dietro un interstiziale di blocco.

Senza questo, la pagina profilo non si dipinge, i controlli DM non si trovano e
il lead viene buttato come 'dm_restricted' — un lead bruciato per ogni contatto,
mentre il bot continua a martellare da dietro l'avviso.
"""
import asyncio

import pytest

from app.browser.instagram_page import InstagramPage
from app.utils.exceptions import AccountChallengeError

WARNING_URL = (
    "https://www.instagram.com/accounts/scraping_warning/?challenge_context=abc&next=%2F"
)


class _FakePage:
    """Pagina minimale: registra i goto e finge il redirect al blocco."""

    def __init__(self, url_dopo_goto: str) -> None:
        self.url = "https://www.instagram.com/"
        self._url_dopo_goto = url_dopo_goto
        self.goto_calls: list[str] = []

    def on(self, event, handler):
        pass

    def remove_listener(self, event, handler):
        pass

    async def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        self.url = self._url_dopo_goto

    def is_closed(self) -> bool:
        return False


def _page_con(monkeypatch, url_dopo_goto: str):
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop)
    page = InstagramPage(None)
    fake = _FakePage(url_dopo_goto)
    page._page = fake

    async def _get_page():
        return fake
    page._get_page = _get_page
    page._account_id = "acc-123"
    return page, fake


def test_send_dm_solleva_challenge_se_redirect_al_warning(monkeypatch):
    page, fake = _page_con(monkeypatch, WARNING_URL)

    with pytest.raises(AccountChallengeError) as exc:
        asyncio.run(page.send_dm(username="mario_rossi", message="ciao"))

    assert exc.value.account_id == "acc-123"
    assert "scraping_warning" in (exc.value.challenge_url or "")
    # Si e' fermato SUBITO dopo il primo goto: nessun tentativo di ricaricare
    # o di cercare i controlli DM da dietro l'avviso.
    assert fake.goto_calls == ["https://www.instagram.com/mario_rossi/"]


def test_un_username_che_contiene_challenge_non_ferma_linvio(monkeypatch):
    # @challenge_accepted e' un profilo normale: send_dm deve proseguire e
    # fallire piu' avanti (per la pagina finta), NON con AccountChallengeError.
    page, _ = _page_con(monkeypatch, "https://www.instagram.com/challenge_accepted/")

    with pytest.raises(Exception) as exc:
        asyncio.run(page.send_dm(username="challenge_accepted", message="ciao"))

    assert not isinstance(exc.value, AccountChallengeError)


def test_ensure_logged_in_memorizza_account_id(monkeypatch):
    page = InstagramPage(None)
    assert page._account_id is None
