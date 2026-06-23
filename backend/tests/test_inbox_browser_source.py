"""Inbox browser source: parsing righe DOM + dedup-frontier (logica pura)."""
import pytest
from app.services.inbox_browser_source import parse_thread_rows, BrowserInboxSource

OWN = 999


def test_parse_rows_one_to_one():
    rows = [{"pk": 123, "username": "mario"}, {"pk": 456, "username": "lucia"}]
    assert parse_thread_rows(rows, OWN) == [(123, "mario"), (456, "lucia")]


def test_parse_rows_skips_self_and_groupish():
    rows = [{"pk": OWN, "username": "me"}, {"pk": None, "username": None}, {"pk": "789", "username": "gino"}]
    assert parse_thread_rows(rows, OWN) == [(789, "gino")]


class FakePage:
    """Simula scroll_inbox_threads: ritorna blocchi di righe finche' esauriti."""
    def __init__(self, blocks):
        self._blocks = list(blocks)
        self.scrolls = 0

    async def scroll_inbox_threads(self):
        self.scrolls += 1
        if not self._blocks:
            return []
        return self._blocks.pop(0)


@pytest.mark.asyncio
async def test_browser_source_paginates_until_empty():
    page = FakePage([
        [{"pk": 1, "username": "a"}, {"pk": 2, "username": "b"}],
        [{"pk": 3, "username": "c"}],
        [],  # nessuna riga nuova -> fine
    ])
    src = BrowserInboxSource(page, OWN)
    p1 = await src.next_page()
    assert p1.participants == [(1, "a"), (2, "b")]
    assert p1.exhausted is False
    p2 = await src.next_page()
    assert p2.participants == [(3, "c")]
    p3 = await src.next_page()
    assert p3.participants == []
    assert p3.exhausted is True
