"""Micro-scroll: probabilistico e non-bloccante; scroll solo entro il ratio.
Sui profili pubblici puo' anche aprire un post (bio_browser_open_post_ratio);
sui privati resta un tocco breve e non apre mai nulla (non c'e' una griglia
di post da guardare)."""
import random
import pytest

from app.services import browser_bio


class _RawPage:
    def __init__(self):
        self.scrolled = 0
        self.post_opened = False
        self.went_back = False

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
        return 1

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
    monkeypatch.setattr(browser_bio.settings, "bio_browser_scroll_ratio", 1.0)
    monkeypatch.setattr(browser_bio.settings, "bio_browser_open_post_ratio", 1.0)
    monkeypatch.setattr(browser_bio.asyncio, "sleep", lambda *_: _noop())
    raw = _RawPage()
    s = _Session(raw)
    did = await browser_bio.maybe_micro_scroll(s, is_private=False, rng=random.Random(1))
    assert did is True
    assert raw.post_opened is True


@pytest.mark.asyncio
async def test_private_profile_no_post(monkeypatch):
    monkeypatch.setattr(browser_bio.settings, "bio_browser_scroll_ratio", 1.0)
    # anche con ratio 1.0 il ramo privato non deve MAI valutare l'apertura post
    monkeypatch.setattr(browser_bio.settings, "bio_browser_open_post_ratio", 1.0)
    monkeypatch.setattr(browser_bio.asyncio, "sleep", lambda *_: _noop())
    raw = _RawPage()
    s = _Session(raw)
    did = await browser_bio.maybe_micro_scroll(s, is_private=True, rng=random.Random(1))
    assert did is True
    assert raw.post_opened is False


async def _noop():
    return None
