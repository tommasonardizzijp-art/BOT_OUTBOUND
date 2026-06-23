"""Sorgente lista contatti dall'inbox DM (scrape_mode=dm_threads).

Espone la funzione pura di estrazione partecipante + l'interfaccia InboxListSource
con le due implementazioni (API/browser). Vedi spec 2026-06-23-inbox-dm-scraping.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Protocol


def extract_thread_participant(thread_users, own_pk: int) -> tuple[int, str] | None:
    """Ritorna (ig_user_id, username) per un thread 1-a-1, None per gruppi/invalidi.

    Perimetro Fase 1: solo 1-a-1 (esattamente un altro utente oltre a noi),
    entrambe le direzioni. `thread_users` puo' contenere o meno l'utente self
    (instagrapi spesso lo esclude); filtriamo own_pk in ogni caso.
    """
    own_pk = int(own_pk)
    others = []
    for u in thread_users or []:
        try:
            pk = int(u.pk)
        except (TypeError, ValueError, AttributeError):
            continue
        if pk == own_pk:
            continue
        username = getattr(u, "username", None)
        if not isinstance(username, str) or not username.strip():
            continue
        others.append((pk, username))
    if len(others) != 1:
        return None
    return others[0]


def _as_users(raw_thread) -> list:
    """Normalizza thread.users sia da oggetti instagrapi sia da dict raw."""
    if isinstance(raw_thread, dict):
        users = raw_thread.get("users", [])
        from types import SimpleNamespace
        return [SimpleNamespace(pk=u.get("pk"), username=u.get("username")) for u in users]
    return getattr(raw_thread, "users", []) or []


def fetch_inbox_page(client, cursor: str | None) -> tuple[list, str | None, bool]:
    """Una pagina dell'inbox via private API. Ritorna (threads, next_cursor, has_older).

    Usa l'endpoint app direct_v2/inbox con thread_message_limit minimo: in Fase 1
    servono solo i partecipanti, non i messaggi -> payload leggero, meno crash parse.
    """
    params = {
        "visual_message_return_type": "unseen",
        "thread_message_limit": "1",
        "persistentBadging": "true",
        "limit": "20",
    }
    if cursor:
        params["cursor"] = cursor
        params["direction"] = "older"
    resp = client.private_request("direct_v2/inbox/", params=params)
    inbox = (resp or {}).get("inbox", {})
    threads = inbox.get("threads", []) or []
    next_cursor = inbox.get("oldest_cursor")
    has_older = bool(inbox.get("has_older"))
    return threads, next_cursor, has_older


class ApiInboxSource:
    """Sorgente inbox via instagrapi private API, paginata a oldest_cursor."""

    def __init__(self, client, own_pk: int, cursor: str | None = None):
        self._client = client
        self._own_pk = int(own_pk)
        self._cursor = cursor

    async def next_page(self) -> InboxPage:
        threads, next_cursor, has_older = await asyncio.to_thread(
            fetch_inbox_page, self._client, self._cursor
        )
        participants: list[tuple[int, str]] = []
        for t in threads:
            p = extract_thread_participant(_as_users(t), self._own_pk)
            if p is not None:
                participants.append(p)
        self._cursor = next_cursor
        exhausted = (not has_older) or (not next_cursor)
        return InboxPage(participants=participants, cursor=next_cursor, exhausted=exhausted)


@dataclass
class InboxPage:
    """Una pagina di partecipanti estratti dall'inbox."""
    participants: list[tuple[int, str]] = field(default_factory=list)
    cursor: str | None = None      # stato di ripresa intra-engine (oldest_cursor o marker)
    exhausted: bool = False        # True quando l'inbox e' stato raggiunto fino all'inizio


class InboxListSource(Protocol):
    """Interfaccia comune alle due sorgenti inbox.

    next_page() restituisce la prossima pagina di partecipanti. La correttezza
    del riavvio (e dello switch engine) e' garantita a monte dal dedup sui
    Follower gia' salvati; cursor/marker sono solo ottimizzazione.
    """
    async def next_page(self) -> InboxPage: ...
