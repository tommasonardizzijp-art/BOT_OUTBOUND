"""Sorgente inbox via browser Patchright: scroll della lista DM /direct/inbox/.

La logica pura (parsing righe, dedup, fine-scroll) e' qui e testabile; i selettori
DOM vivono in InstagramPage.scroll_inbox_threads (da verificare live).
"""
from loguru import logger

from app.services.inbox_source import InboxPage


def parse_thread_rows(rows_data, own_pk: int) -> list[tuple[int, str]]:
    """Da righe DOM (dict pk/username) a lista (ig_user_id, username) 1-a-1 valide."""
    out: list[tuple[int, str]] = []
    for row in rows_data or []:
        if not isinstance(row, dict):
            continue
        pk = row.get("pk")
        username = row.get("username")
        try:
            pk = int(pk)
        except (TypeError, ValueError):
            continue
        if pk == int(own_pk):
            continue
        if not isinstance(username, str) or not username.strip():
            continue
        out.append((pk, username))
    return out


class BrowserInboxSource:
    """Sorgente inbox via scroll del DOM. Ogni next_page() = un blocco di scroll.

    exhausted quando uno scroll non produce piu' righe (lista virtualizzata in fondo).
    La de-duplicazione globale resta a carico di run_inbox_list (existing_ids), qui
    si fa solo de-dup locale per non riemettere le stesse righe ancora a schermo.
    """

    def __init__(self, page, own_pk: int):
        self._page = page
        self._own_pk = int(own_pk)
        self._seen: set[int] = set()

    async def next_page(self) -> InboxPage:
        rows = await self._page.scroll_inbox_threads()
        parsed = parse_thread_rows(rows, self._own_pk)
        fresh: list[tuple[int, str]] = []
        for pk, username in parsed:
            if pk in self._seen:
                continue
            self._seen.add(pk)
            fresh.append((pk, username))
        exhausted = len(rows or []) == 0
        # marker di profondita' best-effort: numero righe viste (non un cursore IG)
        marker = str(len(self._seen))
        return InboxPage(participants=fresh, cursor=marker, exhausted=exhausted)


async def build_browser_inbox_source(db, campaign, account):
    """Apre il browser sull'inbox dell'account e ritorna (source, own_pk, cleanup).

    Usa BrowserSession (long-lived, per-account lock) anziche' get_context/release_context
    che non esistono nel context_manager di questo progetto.
    # VERIFY-LIVE: pk-resolution path — own_pk via instagrapi user_id; sessione browser
    # via BrowserSession.open() che chiama InstagramPage internamente.
    """
    from app.browser.context_manager import BrowserSession
    from app.utils.instagrapi_client import login as _login

    # own_pk: ricavato via instagrapi (login leggero) per coerenza con engine api.
    # VERIFY-LIVE: confermare che client.user_id sia sempre valorizzato dopo login()
    client = await _login(account, db)
    own_pk = int(client.user_id)

    session = BrowserSession(account.id)
    await session.open()

    try:
        pom = session.page  # InstagramPage gia' costruita da BrowserSession.open()
        await pom.ensure_logged_in(account.id)
        await pom.open_inbox()  # naviga a /direct/inbox/ (vedi POM)
    except Exception:
        await session.close()
        raise

    source = BrowserInboxSource(pom, own_pk)

    async def _cleanup():
        await session.close()

    return source, own_pk, _cleanup
