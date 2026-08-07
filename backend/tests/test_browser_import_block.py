"""Il percorso import non deve marcare 'error' i profili quando e' bloccato.

browser_import riusa _capture_web_profile_info, quindi riceve {"__blocked": ...}:
senza un ramo dedicato finirebbe in 'error' e i profili importati sarebbero persi
per un problema che non e' loro.
"""
import asyncio
from types import SimpleNamespace

from app.services.browser_import import resolve_and_store_bio_browser


class _FakeDb:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


def test_blocked_non_marca_il_profilo(monkeypatch):
    async def _capture(raw_page, username, *a, **k):
        return {"__blocked": "scraping_warning"}
    monkeypatch.setattr(
        "app.services.browser_import._capture_web_profile_info", _capture
    )

    row = SimpleNamespace(username="mario_rossi", status="resolving", error=None,
                          updated_at=None)
    db = _FakeDb()

    class _Sess:
        class page:
            @staticmethod
            async def _get_page():
                return object()

    outcome, err = asyncio.run(
        resolve_and_store_bio_browser(row, SimpleNamespace(id="c1"), db, _Sess())
    )

    assert outcome == "blocked"
    assert row.status == "resolving"   # invariato: NON marcato
    assert db.commits == 0             # nessuna scrittura
