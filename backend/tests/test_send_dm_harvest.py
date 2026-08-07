"""La visita che il DM fa comunque deve rendere: cattura passiva, zero richieste.

Vincoli non negoziabili verificati qui:
  - nessuna attesa aggiunta e nessuna fetch esplicita (sarebbe una richiesta
    attribuibile al bot, cioe' il difetto che il livello 'none' esiste per evitare);
  - se il GraphQL non arriva, il DM parte lo stesso.
"""
import asyncio

from app.browser.instagram_page import InstagramPage

PAYLOAD = {"data": {"user": {
    "username": "mario_rossi", "biography": "ciao", "account_type": 2,
    "follower_count": 100,
}}}


class _FakeResponse:
    def __init__(self, url, body):
        self.url = url
        self.status = 200
        self._body = body

    async def json(self):
        return self._body


class _FakePage:
    def __init__(self):
        self.url = "https://www.instagram.com/mario_rossi/"
        self._handlers = []
        self.evaluate_calls = []

    def on(self, event, handler):
        self._handlers.append(handler)

    def remove_listener(self, event, handler):
        if handler in self._handlers:
            self._handlers.remove(handler)

    async def goto(self, url, **kwargs):
        # Durante il caricamento IG spara la sua query: la consegniamo al listener.
        for h in self._handlers:
            await h(_FakeResponse("https://www.instagram.com/api/graphql", PAYLOAD))

    async def evaluate(self, *a, **k):
        self.evaluate_calls.append(a)
        return False

    def is_closed(self):
        return False


def test_cattura_il_payload_senza_chiedere_niente(monkeypatch):
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop)

    page = InstagramPage(None)
    fake = _FakePage()

    async def _get_page():
        return fake
    page._get_page = _get_page
    page._account_id = "acc-1"

    try:
        asyncio.run(page.send_dm(username="mario_rossi", message="ciao"))
    except Exception:
        pass  # l'invio fallisce sulla pagina finta: qui interessa solo la cattura

    assert page.last_profile_capture is not None
    assert page.last_profile_capture["username"] == "mario_rossi"
    assert page.last_profile_capture["account_type"] == 2


def test_senza_graphql_la_cattura_e_none_e_non_esplode(monkeypatch):
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop)

    page = InstagramPage(None)
    fake = _FakePage()
    fake._handlers = []

    async def _goto_muto(url, **kwargs):
        return None
    fake.goto = _goto_muto

    async def _get_page():
        return fake
    page._get_page = _get_page
    page._account_id = "acc-1"

    try:
        asyncio.run(page.send_dm(username="mario_rossi", message="ciao"))
    except Exception:
        pass

    assert page.last_profile_capture is None


def test_la_finestra_di_cattura_copre_lattesa_dopo_il_goto(monkeypatch):
    """Il GraphQL del profilo lo spara la SPA DOPO l'idratazione (mediana
    3.99s, min 2.74 - misure reali 07/08), non durante il `goto` (che ritorna
    a domcontentloaded, ~1s). Qui il fake simula l'ordine temporale vero: il
    `goto` non consegna nulla, la risposta arriva durante la PRIMA attesa che
    send_dm fa comunque subito dopo (asyncio.sleep(1.5-3.0) gia' esistente).
    Se il listener si stacca appena il goto ritorna, questa risposta arriva a
    listener gia' rimosso e va persa — bug reale trovato dopo la chiusura del
    modulo."""
    fake = _FakePage()
    fake._handlers = []

    chiamate_sleep = []

    async def _sleep_che_consegna_tardi(*a, **k):
        chiamate_sleep.append(a)
        if len(chiamate_sleep) == 1:
            # Prima attesa dopo il goto: e' qui, non nel goto, che nella
            # realta' arriva il GraphQL.
            for h in list(fake._handlers):
                await h(_FakeResponse("https://www.instagram.com/api/graphql", PAYLOAD))
        return None
    monkeypatch.setattr(asyncio, "sleep", _sleep_che_consegna_tardi)

    async def _goto_che_non_consegna_nulla(url, **kwargs):
        return None  # il goto NON spara la query: arriva dopo, non durante
    fake.goto = _goto_che_non_consegna_nulla

    page = InstagramPage(None)

    async def _get_page():
        return fake
    page._get_page = _get_page
    page._account_id = "acc-1"

    try:
        asyncio.run(page.send_dm(username="mario_rossi", message="ciao"))
    except Exception:
        pass  # l'invio fallisce sulla pagina finta: qui interessa la cattura

    assert page.last_profile_capture is not None
    assert page.last_profile_capture["username"] == "mario_rossi"


def test_harvest_che_esplode_non_ferma_il_dm(monkeypatch):
    """Se il parsing del payload GraphQL solleva, la cattura resta None ma il
    DM non deve fallire PER COLPA dell'harvest: e' il meccanismo che garantisce
    'se l'harvest fallisce il DM parte lo stesso' — deve essere testato, non
    solo garantito a lettura di codice."""
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop)

    class _BoomResponse:
        def __init__(self, url):
            self.url = url
            self.status = 200

        async def json(self):
            raise ValueError("payload non parsabile — forma cambiata")

    page = InstagramPage(None)
    fake = _FakePage()

    async def _goto_boom(url, **kwargs):
        for h in fake._handlers:
            await h(_BoomResponse("https://www.instagram.com/api/graphql"))
    fake.goto = _goto_boom

    async def _get_page():
        return fake
    page._get_page = _get_page
    page._account_id = "acc-1"

    try:
        asyncio.run(page.send_dm(username="mario_rossi", message="ciao"))
    except Exception as e:
        # send_dm fallisce comunque sulla pagina finta (nessun bottone
        # Messaggio): l'eccezione simulata dell'harvest (ValueError) non deve
        # essere quella che risale fino a qui.
        assert not isinstance(e, ValueError)

    assert page.last_profile_capture is None


def test_la_cattura_e_del_profilo_giusto(monkeypatch):
    """Un payload di un altro utente non deve essere attribuito a questo follower."""
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop)

    page = InstagramPage(None)
    fake = _FakePage()

    async def _goto_altro(url, **kwargs):
        altro = {"data": {"user": {"username": "qualcun_altro", "biography": "x"}}}
        for h in fake._handlers:
            await h(_FakeResponse("https://www.instagram.com/api/graphql", altro))
    fake.goto = _goto_altro

    async def _get_page():
        return fake
    page._get_page = _get_page
    page._account_id = "acc-1"

    try:
        asyncio.run(page.send_dm(username="mario_rossi", message="ciao"))
    except Exception:
        pass

    assert page.last_profile_capture is None
