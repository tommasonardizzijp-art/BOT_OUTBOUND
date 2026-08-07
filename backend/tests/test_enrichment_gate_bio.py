"""Con enrichment_level='none' la Fase Bio non deve partire, su NESSUN motore.

Il livello decide SE si arricchisce, bio_engine decide COME: il gate va a monte
del dispatch, altrimenti andrebbe ripetuto dentro ogni motore.
"""
import asyncio
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("motore", ["api", "browser"])
def test_scrape_bios_non_parte_a_livello_none(monkeypatch, motore):
    from app.services import scrape_bios as mod

    chiamate = []

    async def _mai(*a, **k):
        chiamate.append(a)
        return 1
    monkeypatch.setattr(
        "app.services.browser_bio.enqueue_browser_bio_workers", _mai, raising=False
    )

    campagna = SimpleNamespace(
        id="c1", enrichment_level="none", bio_engine=motore, status="scraping",
    )
    assert mod.enrichment_blocca_la_fase_bio(campagna) is True
    assert chiamate == []


@pytest.mark.parametrize("livello", ["bio", "contacts"])
def test_gli_altri_livelli_non_sono_bloccati(livello):
    from app.services.scrape_bios import enrichment_blocca_la_fase_bio

    campagna = SimpleNamespace(id="c1", enrichment_level=livello, bio_engine="browser")
    assert enrichment_blocca_la_fase_bio(campagna) is False


def test_campagna_senza_il_campo_non_e_bloccata():
    from app.services.scrape_bios import enrichment_blocca_la_fase_bio

    assert enrichment_blocca_la_fase_bio(SimpleNamespace(id="c1")) is False
