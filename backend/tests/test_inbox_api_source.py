"""Inbox API source: paginazione a cursore + estrazione, con client mock."""
import pytest
from types import SimpleNamespace as NS
from app.services.inbox_source import ApiInboxSource

OWN = 999


def _u(pk, username):
    return NS(pk=pk, username=username)


def _thread(*users):
    return NS(users=list(users))


class FakeClient:
    """Mock di instagrapi.Client.private_request per direct_v2/inbox."""
    def __init__(self, pages):
        # pages = list di dict {"threads": [...], "oldest_cursor": str|None, "has_older": bool}
        self._pages = pages
        self.calls = []

    def private_request(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        page = self._pages.pop(0)
        return {
            "inbox": {
                "threads": page["threads"],
                "oldest_cursor": page["oldest_cursor"],
                "has_older": page["has_older"],
            }
        }


def _raw_thread(*users):
    return {"users": [{"pk": pk, "username": un} for pk, un in users]}


@pytest.mark.asyncio
async def test_api_source_first_page_extracts_participants():
    client = FakeClient([
        {"threads": [_raw_thread((123, "mario")), _raw_thread((456, "lucia"), (789, "gino"))],
         "oldest_cursor": "C1", "has_older": True},
    ])
    src = ApiInboxSource(client, OWN)
    page = await src.next_page()
    # gruppo (2 utenti) scartato, resta solo mario
    assert page.participants == [(123, "mario")]
    assert page.cursor == "C1"
    assert page.exhausted is False


@pytest.mark.asyncio
async def test_api_source_passes_cursor_and_detects_end():
    client = FakeClient([
        {"threads": [_raw_thread((123, "mario"))], "oldest_cursor": "C1", "has_older": True},
        {"threads": [_raw_thread((321, "anna"))], "oldest_cursor": None, "has_older": False},
    ])
    src = ApiInboxSource(client, OWN)
    p1 = await src.next_page()
    assert p1.exhausted is False
    p2 = await src.next_page()
    assert p2.participants == [(321, "anna")]
    assert p2.exhausted is True
    # la 2a chiamata deve aver passato il cursore della 1a
    assert client.calls[1][1].get("cursor") == "C1"
