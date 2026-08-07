# backend/tests/test_bio_block_detect.py
"""Dietro un interstiziale di blocco, la Fase Bio non deve marcare nulla.

Il difetto che questo test previene: ogni profilo torna vuoto -> outcome
'not_found' -> follower marcato 'skipped' -> mai piu' pescato dal worker DM.
Una sessione dietro l'avviso brucia l'intera lista senza un solo errore.
"""
import asyncio

from app.services.browser_bio import _capture_web_profile_info

WARNING_URL = "https://www.instagram.com/accounts/scraping_warning/?challenge_context=abc"


class _FakeRawPage:
    def __init__(self, url_dopo_goto: str) -> None:
        self.url = "https://www.instagram.com/"
        self._url_dopo_goto = url_dopo_goto
        self.evaluate_calls: list = []

    def on(self, event, handler):
        pass

    def remove_listener(self, event, handler):
        pass

    async def goto(self, url, **kwargs):
        self.url = self._url_dopo_goto

    async def evaluate(self, script, args=None):
        # Se ci arriviamo da dietro l'avviso e' un difetto: significa che il
        # codice sta ancora facendo richieste esplicite mentre e' bloccato.
        self.evaluate_calls.append(script)
        return None


def test_capture_segnala_il_blocco_e_non_chiede_altro(monkeypatch):
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop)

    page = _FakeRawPage(WARNING_URL)
    result = asyncio.run(_capture_web_profile_info(page, "mario_rossi", timeout_s=0.1))

    assert result == {"__blocked": "scraping_warning"}
    # Nessuna fetch esplicita da dietro l'avviso.
    assert page.evaluate_calls == []


def test_profilo_normale_non_segnala_blocco(monkeypatch):
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop)

    page = _FakeRawPage("https://www.instagram.com/challenge_accepted/")
    result = asyncio.run(_capture_web_profile_info(page, "challenge_accepted", timeout_s=0.1))

    assert result != {"__blocked": "scraping_warning"}
    assert not (isinstance(result, dict) and result.get("__blocked"))
