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
    # I primi tre valori: il quarto (fondo_dichiarato) ha i suoi test dedicati
    # in test_inbox_api_dedup_username.py.
    return fetch_inbox_page(SyncClient(resp), cursor=None)[:3]


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
    """{'inbox': None} — must degrade gracefully (DEFECT #1 fixed).

    (resp or {}).get('inbox') or {} now returns {} when the key is present
    but null, preventing AttributeError on the subsequent .get('threads') call.
    """
    threads, cursor, has_older = _fetch({"inbox": None})
    assert threads == []
    assert cursor is None
    assert has_older is False


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
    """threads is a dict (not a list) — type guard must return [] (DEFECT #2 fixed).

    isinstance(_threads, list) is False for a dict, so fetch_inbox_page now
    returns [] instead of passing the raw dict through to the caller.
    """
    threads, cursor, has_older = _fetch(
        {"inbox": {"threads": {"key": "val"}, "oldest_cursor": None, "has_older": False}}
    )
    assert threads == []
    assert has_older is False


def test_fetch_threads_is_int():
    """threads=42 — type guard must return [] (DEFECT #2 fixed).

    isinstance(42, list) is False, so fetch_inbox_page now returns [] instead
    of passing the non-iterable integer through to the caller.
    """
    threads, cursor, has_older = _fetch(
        {"inbox": {"threads": 42, "oldest_cursor": None, "has_older": False}}
    )
    assert threads == []
    assert has_older is False


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
    """Client returns {'inbox': None} — must not raise (DEFECT #1 fixed).

    With the 'or {}' guard, None is replaced by {}, so threads/cursor/has_older
    all default to empty/False and next_page returns a fully exhausted InboxPage.
    """
    client = HostileClient([{"inbox": None}])
    src = ApiInboxSource(client, OWN)
    page = await src.next_page()
    assert page.participants == []
    assert page.exhausted is True


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
    """threads=42 — must not raise (DEFECT #2 fixed).

    The isinstance guard in fetch_inbox_page converts 42 to [], so next_page
    iterates an empty list and returns an exhausted InboxPage with no participants.
    """
    client = HostileClient([
        {"inbox": {"threads": 42, "oldest_cursor": None, "has_older": False}}
    ])
    src = ApiInboxSource(client, OWN)
    page = await src.next_page()
    assert page.participants == []
    assert page.exhausted is True


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
    """Dict-thread where users=None — must not raise (DEFECT #3 fixed).

    _as_users now uses 'raw_thread.get("users") or []' so None is replaced by
    [], extract_thread_participant returns None (no others), thread is skipped.
    """
    client = HostileClient([{
        "inbox": {
            "threads": [{"users": None}],
            "oldest_cursor": None,
            "has_older": False,
        }
    }])
    src = ApiInboxSource(client, OWN)
    page = await src.next_page()
    assert page.participants == []
    assert page.exhausted is True


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


# ── Utenti leggibili: la misura che alimenta la guardia sui payload degradati ──
# Trovato in review: la guardia esisteva ma NESSUN test la copriva — rimettendo il
# bug non diventava rosso niente. `threads_con_utenti` e' prodotto qui, quindi e'
# qui che va provato.

class _ClientFinto:
    def __init__(self, payload):
        self._payload = payload

    def private_request(self, path, params=None):
        return self._payload


def _pagina_da(threads):
    import asyncio as _asyncio
    from app.services.inbox_source import ApiInboxSource
    src = ApiInboxSource(_ClientFinto({"inbox": {"threads": threads,
                                                 "has_older": True,
                                                 "oldest_cursor": "C1"}}), own_pk=999)
    return _asyncio.run(src.next_page())


def test_utenti_col_pk_ma_SENZA_username_non_contano_come_leggibili():
    """`extract_thread_participant` scarta un utente se manca il pk OPPURE lo
    username. Se la misura contasse il solo pk, un payload degradato con i pk e
    senza username lascerebbe muta la guardia — e il giro chiuderebbe con "inbox
    gia' tutto raccolto", esattamente il messaggio che quella guardia esiste per
    evitare."""
    pagina = _pagina_da([
        {"users": [{"pk": 111, "username": None}]},
        {"users": [{"pk": 222}]},
        {"users": [{"pk": 333, "username": "   "}]},
    ])
    assert pagina.threads_letti == 3
    assert pagina.threads_con_utenti == 0, \
        "senza username non c'e' niente da estrarre: non sono utenti leggibili"
    assert pagina.participants == []


def test_un_thread_di_gruppo_conta_come_leggibile():
    """La distinzione che tiene in piedi tutto: nei gruppi gli utenti CI SONO,
    sono solo piu' di uno. Va contato come leggibile, o la guardia fermerebbe un
    tratto di chat di gruppo del tutto legittimo."""
    pagina = _pagina_da([
        {"users": [{"pk": 1, "username": "a"}, {"pk": 2, "username": "b"}]},
    ])
    assert pagina.participants == [], "un gruppo non da' partecipanti 1-a-1"
    assert pagina.threads_con_utenti == 1, "ma gli utenti ci sono: e' leggibile"


def test_users_con_elementi_non_dict_non_fa_sollevare():
    """Il payload piu' rotto era l'unico che la guardia non vedeva: elementi non
    dict dentro `users` facevano sollevare l'intera pagina invece di essere
    contati come "niente da estrarre"."""
    pagina = _pagina_da([
        {"users": [42, "x", None]},
        {"users": "non-una-lista"},
    ])
    assert pagina.threads_letti == 2
    assert pagina.threads_con_utenti == 0
    assert pagina.participants == []
