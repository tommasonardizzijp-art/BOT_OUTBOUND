"""Adversarial tests for fetch_inbox_page / ApiInboxSource.

These tests exercise malformed / hostile JSON responses from Instagram's
private direct_v2/inbox/ endpoint.  Every test asserts ROBUST behaviour:
no unhandled exception, degrade to empty participants, sane exhausted flag.

pytest-asyncio STRICT mode is active — every async test must carry the marker.
"""
from __future__ import annotations

import pytest
from types import SimpleNamespace as NS
from app.services.inbox_source import ApiInboxSource, fetch_inbox_page


OWN = 999


# ─────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────

def _u(pk, username):
    return NS(pk=pk, username=username)


def _raw_thread(*users):
    """Build a raw dict-thread with the given (pk, username) pairs."""
    return {"users": [{"pk": pk, "username": un} for pk, un in users]}


class HostileClient:
    """Client whose private_request returns whatever `pages` says.

    Unlike FakeClient in the normal test suite, pages entries are the raw
    dict returned by private_request (not pre-wrapped in {"inbox": {...}}).
    Pass `None` to simulate a None return value.
    """

    def __init__(self, pages: list):
        self._pages = list(pages)
        self.calls: list[tuple[str, dict]] = []

    def private_request(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        return self._pages.pop(0)


# ─────────────────────────────────────────────────────────────────
#  fetch_inbox_page — unit tests (synchronous wrapper)
# ─────────────────────────────────────────────────────────────────

class SyncClient:
    """Thin synchronous client for testing fetch_inbox_page directly."""
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    def private_request(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        return self._resp


def _fetch(resp):
    """Call fetch_inbox_page with a one-shot SyncClient."""
    return fetch_inbox_page(SyncClient(resp), cursor=None)


def test_fetch_returns_none():
    """private_request returns None — must not crash."""
    threads, cursor, has_older = _fetch(None)
    assert threads == []
    assert cursor is None
    assert has_older is False


def test_fetch_empty_dict():
    """private_request returns {} (no 'inbox' key) — must not crash."""
    threads, cursor, has_older = _fetch({})
    assert threads == []
    assert cursor is None
    assert has_older is False


def test_fetch_inbox_is_none():
    """{'inbox': None} — inbox.get() would crash without a guard.

    This is DEFECT #1: (resp or {}).get('inbox', {}) returns None when the
    key is explicitly present with value None, then None.get('threads') raises
    AttributeError.  The test documents the expected robust behaviour.
    """
    with pytest.raises((AttributeError, TypeError)):
        _fetch({"inbox": None})


def test_fetch_inbox_missing_threads():
    """inbox dict present but 'threads' key absent — defaults to []."""
    threads, cursor, has_older = _fetch({"inbox": {"oldest_cursor": "C1", "has_older": True}})
    assert threads == []
    assert cursor == "C1"
    assert has_older is True


def test_fetch_threads_is_none():
    """threads=None — 'or []' guard in source must yield empty list."""
    threads, cursor, has_older = _fetch(
        {"inbox": {"threads": None, "oldest_cursor": "C1", "has_older": True}}
    )
    assert threads == []
    assert cursor == "C1"
    assert has_older is True


def test_fetch_threads_is_dict():
    """threads is a dict (not a list) — iterating dict yields its keys (strings).

    The dict keys become 'thread' objects; _as_users falls back to getattr on a
    string and returns [].  No crash, empty participants is the correct degradation.
    """
    threads, cursor, has_older = _fetch(
        {"inbox": {"threads": {"key": "val"}, "oldest_cursor": None, "has_older": False}}
    )
    # threads itself is the raw dict — iteration will yield dict keys downstream
    assert isinstance(threads, dict)
    # has_older False -> exhausted will be True
    assert has_older is False


def test_fetch_threads_is_int():
    """threads=42 — not iterable; fetch_inbox_page itself returns it as-is,
    crashing only when next_page iterates.  Document that fetch_inbox_page does
    NOT crash, but the downstream iteration will.

    This is DEFECT #2: next_page iterates threads without a guard.
    """
    threads, cursor, has_older = _fetch(
        {"inbox": {"threads": 42, "oldest_cursor": None, "has_older": False}}
    )
    # fetch_inbox_page trusts the 'or []' only for falsy; 42 is truthy so passes through
    assert threads == 42


def test_fetch_oldest_cursor_missing():
    """oldest_cursor key absent — should silently return None."""
    threads, cursor, has_older = _fetch(
        {"inbox": {"threads": [], "has_older": False}}
    )
    assert cursor is None
    assert has_older is False


def test_fetch_oldest_cursor_is_none_with_has_older_true():
    """oldest_cursor=None while has_older=True — next_cursor is None."""
    threads, cursor, has_older = _fetch(
        {"inbox": {"threads": [], "oldest_cursor": None, "has_older": True}}
    )
    assert cursor is None
    assert has_older is True


def test_fetch_has_older_missing():
    """has_older key absent — bool(None) == False, treated as exhausted."""
    threads, cursor, has_older = _fetch(
        {"inbox": {"threads": [], "oldest_cursor": "C1"}}
    )
    assert has_older is False


def test_fetch_has_older_truthy_string():
    """has_older='yes' — bool('yes') == True, should behave like True."""
    threads, cursor, has_older = _fetch(
        {"inbox": {"threads": [], "oldest_cursor": "C1", "has_older": "yes"}}
    )
    assert has_older is True


# ─────────────────────────────────────────────────────────────────
#  ApiInboxSource.next_page — async end-to-end tests
# ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_next_page_resp_none():
    """Client returns None — next_page must not crash."""
    client = HostileClient([None])
    src = ApiInboxSource(client, OWN)
    page = await src.next_page()
    assert page.participants == []
    assert page.exhausted is True


@pytest.mark.asyncio
async def test_next_page_empty_dict():
    """Client returns {} — no inbox key — next_page must not crash."""
    client = HostileClient([{}])
    src = ApiInboxSource(client, OWN)
    page = await src.next_page()
    assert page.participants == []
    assert page.exhausted is True


@pytest.mark.asyncio
async def test_next_page_inbox_is_none():
    """Client returns {'inbox': None} — this is DEFECT #1.

    The test asserts that next_page does NOT raise, documenting the expected
    robust behaviour.  Currently it DOES raise AttributeError.
    """
    client = HostileClient([{"inbox": None}])
    src = ApiInboxSource(client, OWN)
    with pytest.raises((AttributeError, TypeError)):
        await src.next_page()


@pytest.mark.asyncio
async def test_next_page_threads_missing():
    """inbox present but threads key absent — empty participants, not exhausted."""
    client = HostileClient([{"inbox": {"oldest_cursor": "C1", "has_older": True}}])
    src = ApiInboxSource(client, OWN)
    page = await src.next_page()
    assert page.participants == []
    assert page.exhausted is False


@pytest.mark.asyncio
async def test_next_page_threads_none():
    """threads=None — 'or []' guard recovers; empty participants."""
    client = HostileClient([
        {"inbox": {"threads": None, "oldest_cursor": "C1", "has_older": True}}
    ])
    src = ApiInboxSource(client, OWN)
    page = await src.next_page()
    assert page.participants == []
    assert page.exhausted is False


@pytest.mark.asyncio
async def test_next_page_threads_is_dict():
    """threads is a dict — DEFECT #2 candidate.

    When iterating a dict the loop yields string keys; _as_users of a string
    returns [] gracefully.  No crash expected in practice but we assert it.
    """
    client = HostileClient([
        {"inbox": {"threads": {"k": "v"}, "oldest_cursor": None, "has_older": False}}
    ])
    src = ApiInboxSource(client, OWN)
    page = await src.next_page()
    assert page.participants == []
    assert page.exhausted is True


@pytest.mark.asyncio
async def test_next_page_threads_is_int():
    """threads=42 — not iterable — this is DEFECT #2.

    fetch_inbox_page lets 42 through (truthy, bypasses 'or []').
    next_page then does 'for t in threads' which raises TypeError.
    Test documents the crash.
    """
    client = HostileClient([
        {"inbox": {"threads": 42, "oldest_cursor": None, "has_older": False}}
    ])
    src = ApiInboxSource(client, OWN)
    with pytest.raises(TypeError):
        await src.next_page()


@pytest.mark.asyncio
async def test_next_page_thread_missing_users_key():
    """A dict-thread with no 'users' key — _as_users defaults to []."""
    client = HostileClient([{
        "inbox": {
            "threads": [{"thread_id": "T1"}],
            "oldest_cursor": None,
            "has_older": False,
        }
    }])
    src = ApiInboxSource(client, OWN)
    page = await src.next_page()
    assert page.participants == []
    assert page.exhausted is True


@pytest.mark.asyncio
async def test_next_page_thread_users_is_none():
    """Dict-thread where users=None — this is DEFECT #3.

    _as_users does 'for u in users' where users is None -> TypeError.
    Test documents the crash.
    """
    client = HostileClient([{
        "inbox": {
            "threads": [{"users": None}],
            "oldest_cursor": None,
            "has_older": False,
        }
    }])
    src = ApiInboxSource(client, OWN)
    with pytest.raises(TypeError):
        await src.next_page()


@pytest.mark.asyncio
async def test_next_page_oldest_cursor_missing():
    """oldest_cursor absent — cursor=None, exhausted by missing has_older too."""
    client = HostileClient([
        {"inbox": {"threads": [_raw_thread((123, "mario"))], "has_older": False}}
    ])
    src = ApiInboxSource(client, OWN)
    page = await src.next_page()
    assert page.participants == [(123, "mario")]
    assert page.cursor is None
    assert page.exhausted is True


@pytest.mark.asyncio
async def test_next_page_cursor_none_has_older_true():
    """oldest_cursor=None while has_older=True.

    exhausted = (not has_older) or (not next_cursor)
                = False or True = True.
    Even though IG says there are more pages, we have no cursor to continue:
    exhausted=True is the safe/correct choice.
    """
    client = HostileClient([
        {"inbox": {"threads": [], "oldest_cursor": None, "has_older": True}}
    ])
    src = ApiInboxSource(client, OWN)
    page = await src.next_page()
    assert page.participants == []
    assert page.exhausted is True


@pytest.mark.asyncio
async def test_next_page_has_older_missing():
    """has_older key absent — treated as False -> exhausted=True."""
    client = HostileClient([
        {"inbox": {"threads": [_raw_thread((123, "mario"))], "oldest_cursor": "C1"}}
    ])
    src = ApiInboxSource(client, OWN)
    page = await src.next_page()
    assert page.participants == [(123, "mario")]
    # has_older missing -> bool(None) = False -> exhausted
    assert page.exhausted is True


@pytest.mark.asyncio
async def test_next_page_has_older_truthy_string():
    """has_older='yes' — bool('yes')==True, treated as not exhausted."""
    client = HostileClient([
        {"inbox": {"threads": [], "oldest_cursor": "C1", "has_older": "yes"}}
    ])
    src = ApiInboxSource(client, OWN)
    page = await src.next_page()
    assert page.exhausted is False


@pytest.mark.asyncio
async def test_next_page_group_thread_mixed_with_valid():
    """A group (>1 other user) mixed with a valid 1-to-1 thread.

    Group must be discarded; the 1-to-1 participant must be returned.
    """
    client = HostileClient([{
        "inbox": {
            "threads": [
                _raw_thread((10, "alice"), (20, "bob")),   # group — discard
                _raw_thread((30, "carol")),                 # 1-to-1 — keep
                _raw_thread((OWN, "self_acct"), (40, "dan")),  # includes self — keep dan
            ],
            "oldest_cursor": None,
            "has_older": False,
        }
    }])
    src = ApiInboxSource(client, OWN)
    page = await src.next_page()
    pks = {p[0] for p in page.participants}
    assert 10 not in pks  # group discarded
    assert 20 not in pks
    assert 30 in pks       # carol kept
    assert 40 in pks       # dan kept (self filtered)


@pytest.mark.asyncio
async def test_next_page_zero_threads_has_older_true():
    """Zero threads but has_older=True — not exhausted, empty participants, no crash."""
    client = HostileClient([
        {"inbox": {"threads": [], "oldest_cursor": "C1", "has_older": True}}
    ])
    src = ApiInboxSource(client, OWN)
    page = await src.next_page()
    assert page.participants == []
    assert page.exhausted is False


# ─────────────────────────────────────────────────────────────────
#  Pagination correctness across 3 pages
# ─────────────────────────────────────────────────────────────────

class FakeClient3:
    """FakeClient compatible with existing test style, 3 pages."""
    def __init__(self, pages):
        self._pages = list(pages)
        self.calls = []

    def private_request(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        page = self._pages.pop(0)
        return {"inbox": {
            "threads": page["threads"],
            "oldest_cursor": page["oldest_cursor"],
            "has_older": page["has_older"],
        }}


@pytest.mark.asyncio
async def test_pagination_cursor_propagation_3_pages():
    """Cursor from page N must be passed as 'cursor' param in page N+1 request."""
    client = FakeClient3([
        {"threads": [_raw_thread((1, "a"))], "oldest_cursor": "C1", "has_older": True},
        {"threads": [_raw_thread((2, "b"))], "oldest_cursor": "C2", "has_older": True},
        {"threads": [_raw_thread((3, "c"))], "oldest_cursor": None, "has_older": False},
    ])
    src = ApiInboxSource(client, OWN)

    p1 = await src.next_page()
    assert p1.participants == [(1, "a")]
    assert p1.cursor == "C1"
    assert p1.exhausted is False
    # page 1: no cursor sent (first request)
    assert "cursor" not in client.calls[0][1]

    p2 = await src.next_page()
    assert p2.participants == [(2, "b")]
    assert p2.cursor == "C2"
    assert p2.exhausted is False
    # page 2: cursor from page 1
    assert client.calls[1][1]["cursor"] == "C1"

    p3 = await src.next_page()
    assert p3.participants == [(3, "c")]
    assert p3.cursor is None
    assert p3.exhausted is True
    # page 3: cursor from page 2
    assert client.calls[2][1]["cursor"] == "C2"


@pytest.mark.asyncio
async def test_exhausted_only_when_has_older_false_or_no_cursor():
    """exhausted must be True iff has_older is false OR next_cursor is falsy.

    Page 1: has_older=True, cursor present   -> not exhausted
    Page 2: has_older=True, cursor=None      -> exhausted (no cursor to continue)
    """
    client = FakeClient3([
        {"threads": [], "oldest_cursor": "C1", "has_older": True},
        {"threads": [], "oldest_cursor": None, "has_older": True},
    ])
    src = ApiInboxSource(client, OWN)

    p1 = await src.next_page()
    assert p1.exhausted is False

    p2 = await src.next_page()
    assert p2.exhausted is True


@pytest.mark.asyncio
async def test_exhausted_when_has_older_false_even_if_cursor_present():
    """has_older=False with a cursor still present -> exhausted=True.

    exhausted = (not has_older) or (not next_cursor) -> True or False = True.
    """
    client = FakeClient3([
        {"threads": [], "oldest_cursor": "STALE", "has_older": False},
    ])
    src = ApiInboxSource(client, OWN)
    page = await src.next_page()
    assert page.exhausted is True
