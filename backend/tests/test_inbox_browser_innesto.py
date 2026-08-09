# backend/tests/test_inbox_browser_innesto.py
"""Il bivio: 'api' va dove e' sempre andato, 'browser' al motore nuovo.

Il test sul percorso API e' una guardia di NON REGRESSIONE: il motore esistente
non deve cambiare comportamento.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.models.campaign import CampaignStatus


def _campagna(engine):
    return SimpleNamespace(
        id="c1", scrape_mode="dm_threads", inbox_engine=engine,
        status=CampaignStatus.listing,
    )


@pytest.mark.asyncio
async def test_engine_api_va_al_motore_esistente():
    from app.services import scrape_list
    with patch("app.services.scrape_inbox.run_inbox_list", new=AsyncMock(return_value=None)) as api, \
         patch("app.services.scrape_inbox_browser.run_inbox_browser_list", new=AsyncMock()) as browser:
        await scrape_list._dispatch_inbox("c1", None, _campagna("api"))
        api.assert_awaited_once()
        browser.assert_not_awaited()


@pytest.mark.asyncio
async def test_engine_browser_va_al_motore_nuovo():
    from app.services import scrape_list
    with patch("app.services.scrape_inbox.run_inbox_list", new=AsyncMock()) as api, \
         patch("app.services.scrape_inbox_browser.run_inbox_browser_list", new=AsyncMock(return_value=None)) as browser:
        await scrape_list._dispatch_inbox("c1", None, _campagna("browser"))
        browser.assert_awaited_once()
        api.assert_not_awaited()


@pytest.mark.asyncio
async def test_engine_assente_usa_api():
    """Retrocompatibilita': una campagna senza il campo non deve cambiare motore."""
    from app.services import scrape_list
    campagna = SimpleNamespace(id="c1", scrape_mode="dm_threads", status=CampaignStatus.listing)
    with patch("app.services.scrape_inbox.run_inbox_list", new=AsyncMock(return_value=None)) as api:
        await scrape_list._dispatch_inbox("c1", None, campagna)
        api.assert_awaited_once()
