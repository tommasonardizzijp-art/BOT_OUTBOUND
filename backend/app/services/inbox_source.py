"""Sorgente lista contatti dall'inbox DM (scrape_mode=dm_threads).

Espone la funzione pura di estrazione partecipante + l'interfaccia InboxListSource
con le due implementazioni (API/browser). Vedi spec 2026-06-23-inbox-dm-scraping.
"""
from __future__ import annotations

import asyncio
import time
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
        users = raw_thread.get("users") or []
        from types import SimpleNamespace
        return [SimpleNamespace(pk=u.get("pk"), username=u.get("username")) for u in users]
    return getattr(raw_thread, "users", []) or []


PAGINA_ATTESA = 20
"""Thread richiesti a IG per pagina (`limit`). Serve anche a valle: una pagina
PIENA che dichiara il fondo e' una contraddizione, e una piu' corta senza
fondo e' il sintomo tipico di un freno silenzioso."""


def fetch_inbox_page(client, cursor: str | None) -> tuple[list, str | None, bool, bool]:
    """Una pagina dell'inbox via private API.

    Ritorna (threads, next_cursor, has_older, fondo_dichiarato).

    I parametri replicano ESATTAMENTE quelli che l'app mobile invia (verificati sul
    sorgente instagrapi, mixins/direct.py::direct_threads_chunk, il riferimento di
    reverse-engineering che genera il nostro traffico). Ogni deviazione rende la
    richiesta distinguibile dal client reale = rischio checkpoint. In particolare
    thread_message_limit=10 (non 1), is_prefetching e fetch_reason presenti.
    Estraiamo solo i partecipanti dai thread grezzi: i messaggi non li leggiamo,
    quindi il payload piu' grande non costa parse extra.
    """
    params = {
        "visual_message_return_type": "unseen",
        "thread_message_limit": "10",
        "persistentBadging": "true",
        "limit": str(PAGINA_ATTESA),
        "is_prefetching": "false",
    }
    if cursor:
        params["cursor"] = cursor
        params["direction"] = "older"
        params["fetch_reason"] = "page_scroll"
    resp = client.private_request("direct_v2/inbox/", params=params)
    inbox = (resp or {}).get("inbox") or {}
    _threads = inbox.get("threads")
    threads = _threads if isinstance(_threads, list) else []
    next_cursor = inbox.get("oldest_cursor")
    _has_older = inbox.get("has_older")
    has_older = bool(_has_older)
    # `fondo_dichiarato` NON e' `not has_older`: una risposta degradata (chiave
    # assente, corpo parziale, soft-block) darebbe has_older=None e quindi "fondo
    # raggiunto" — e il fondo alza un interruttore PERMANENTE sulla campagna. Serve
    # che IG lo dica: has_older esattamente False, e un corpo che contenga davvero
    # la lista threads (stessa difesa di `_threads` qui sopra).
    fondo_dichiarato = (_has_older is False) and isinstance(_threads, list)
    return threads, next_cursor, has_older, fondo_dichiarato


class ApiInboxSource:
    """Sorgente inbox via instagrapi private API, paginata a oldest_cursor."""

    def __init__(self, client, own_pk: int, cursor: str | None = None):
        self._client = client
        self._own_pk = int(own_pk)
        self._cursor = cursor

    async def next_page(self) -> InboxPage:
        _t0 = time.monotonic()
        threads, next_cursor, has_older, fondo_dichiarato = await asyncio.to_thread(
            fetch_inbox_page, self._client, self._cursor
        )
        latenza_ms = int((time.monotonic() - _t0) * 1000)
        participants: list[tuple[int, str]] = []
        # Thread che portano ALMENO un utente con pk leggibile, anche se poi il
        # filtro 1-a-1 li scarta. Serve a distinguere due casi che dall'esterno
        # danno lo stesso segnale ("zero partecipanti") ma sono opposti:
        #   - tratto di chat di GRUPPO  -> gli utenti ci sono, sono solo troppi.
        #     Legittimo, la discesa deve proseguire.
        #   - payload DEGRADATO         -> gli utenti non ci sono proprio.
        #     Non c'e' niente da estrarre: fermarsi e dirlo.
        threads_con_utenti = 0
        for t in threads:
            utenti = _as_users(t)
            if any(getattr(u, "pk", None) is not None for u in (utenti or [])):
                threads_con_utenti += 1
            p = extract_thread_participant(utenti, self._own_pk)
            if p is not None:
                participants.append(p)
        self._cursor = next_cursor
        exhausted = (not has_older) or (not next_cursor)
        # `exhausted` ferma il giro anche quando manca solo il cursore (payload
        # troncato, blip): giusto per fermarsi, sbagliato per dichiarare che
        # l'inbox e' finito. Il fondo lo dichiara SOLO has_older=False, che e'
        # l'unica cosa che IG dice davvero — vedi inbox_bottom_reached, che e' un
        # interruttore permanente e non va alzato su un forse.
        return InboxPage(
            participants=participants, cursor=next_cursor, exhausted=exhausted,
            bottom_confirmed=fondo_dichiarato, threads_letti=len(threads),
            has_older=has_older, latenza_ms=latenza_ms,
            threads_con_utenti=threads_con_utenti,
        )


@dataclass
class InboxPage:
    """Una pagina di partecipanti estratti dall'inbox."""
    participants: list[tuple[int, str]] = field(default_factory=list)
    cursor: str | None = None      # stato di ripresa intra-engine (oldest_cursor o marker)
    exhausted: bool = False        # True quando non si puo' proseguire (fondo O cursore mancante)
    bottom_confirmed: bool = False  # True SOLO se IG ha detto has_older=False: il fondo vero
    # Thread GREZZI nella pagina, prima del filtro 1-a-1: un tratto di soli gruppi
    # da' zero partecipanti ma non e' una pagina vuota, e non deve far credere che
    # la discesa sia finita.
    threads_letti: int = 0
    # Cosa ha risposto IG su "sotto c'e' altro": serve nel log per distinguere una
    # discesa che si e' fermata da una che e' arrivata in fondo.
    has_older: bool = False
    # Quanto ha impiegato la chiamata. Una latenza che raddoppia e' il primo
    # sintomo leggibile di un freno lato server.
    latenza_ms: int | None = None
    # Thread che portavano almeno un utente leggibile (anche se scartati dal filtro
    # 1-a-1). A zero su una pagina piena di thread significa payload senza dati
    # utente: niente da estrarre, e va distinto da un tratto di gruppi.
    #
    # `None` = NON MISURATO, e non va confuso con zero: e' il valore delle pagine
    # costruite a mano (test, sorgenti alternative) che non hanno un payload da
    # ispezionare. Su "non misurato" la guardia a valle NON scatta — sconosciuto
    # non e' un'accusa. Solo la sorgente API vera lo valorizza.
    threads_con_utenti: int | None = None


class InboxListSource(Protocol):
    """Interfaccia comune alle due sorgenti inbox.

    next_page() restituisce la prossima pagina di partecipanti. La correttezza
    del riavvio (e dello switch engine) e' garantita a monte dal dedup sui
    Follower gia' salvati; cursor/marker sono solo ottimizzazione.
    """
    async def next_page(self) -> InboxPage: ...
