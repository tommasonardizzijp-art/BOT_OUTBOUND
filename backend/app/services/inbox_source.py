"""Sorgente lista contatti dall'inbox DM (scrape_mode=dm_threads).

Espone la funzione pura di estrazione partecipante + l'interfaccia InboxListSource
con le due implementazioni (API/browser). Vedi spec 2026-06-23-inbox-dm-scraping.
"""
from __future__ import annotations

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
