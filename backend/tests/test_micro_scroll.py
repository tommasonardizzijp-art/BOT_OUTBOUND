"""Micro-scroll: probabilistico e non-bloccante; scroll solo entro il ratio."""
import random
import pytest

from app.services import browser_bio


class _RawPage:
    def __init__(self): self.scrolled = 0
    async def evaluate(self, *a, **k): self.scrolled += 1


class _Page:
    def __init__(self, raw): self._raw = raw
    async def _get_page(self): return self._raw


class _Session:
    def __init__(self): self.page = _Page(_RawPage())


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


async def _noop():
    return None
