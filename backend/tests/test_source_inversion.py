"""Passo 4 / A.1 — inversione delle sorgenti dati profilo.

Invariante che questi test proteggono: quando la pagina produce da se' il payload
GraphQL (misurato 65/65 sul campo), `_capture_web_profile_info` lo usa e NON fa
nessuna richiesta esplicita. Prima dell'inversione il codice attendeva
`web_profile_info` (misurato 0/65) per tutto il timeout e poi faceva una fetch
in-page: una richiesta attribuibile per profilo che la pagina non avrebbe fatto.

PERCHE' IL FAKE CONSEGNA IN RITARDO E NON DENTRO IL `goto`: il fake di
`test_graphql_fallback_capture.py` emette tutte le response dentro il finto
`goto`, quindi al primo giro del polling tutto e' gia' catturato e la variabile
TEMPO — che e' esattamente cio' che l'inversione cambia — non esiste piu'. Un
fake cosi' fa passare sia il codice vecchio sia quello nuovo. Qui le response
arrivano DOPO che `goto` e' tornato, con un ritardo, come nel browser vero.
"""
import asyncio
import time

import pytest

from app.services import browser_bio
from app.services.browser_bio import _capture_web_profile_info

BLOCK_URL = "https://www.instagram.com/accounts/scraping_warning/"


class FakeResponse:
    def __init__(self, url, status, payload):
        self.url = url
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload


class FakePageRitardata:
    """Come il Playwright vero: `goto` ritorna appena la pagina e' navigabile, e
    le response arrivano DOPO, ognuna al proprio ritardo.

    `responses` e' una lista di (ritardo_s, FakeResponse). `redirect_to` simula
    l'atterraggio su un interstiziale IG.
    """

    def __init__(self, responses, evaluate_result=None, redirect_to=None):
        self.responses = responses
        self.evaluate_result = evaluate_result
        self.redirect_to = redirect_to
        self._on_response = None
        self.evaluate_calls = []
        self.url = "https://www.instagram.com/"
        self._tasks: list[asyncio.Task] = []

    def on(self, event, cb):
        if event == "response":
            self._on_response = cb

    def remove_listener(self, event, cb):
        if event == "response" and self._on_response is cb:
            self._on_response = None

    async def goto(self, url, **kw):
        self.url = self.redirect_to or url

        async def _consegna(ritardo, resp):
            await asyncio.sleep(ritardo)
            if self._on_response is not None:
                await self._on_response(resp)

        for ritardo, resp in self.responses:
            self._tasks.append(asyncio.create_task(_consegna(ritardo, resp)))

    async def evaluate(self, script, args):
        self.evaluate_calls.append(args)
        return self.evaluate_result


def _gql(username, pk="999"):
    return FakeResponse(
        "https://www.instagram.com/api/graphql",
        200,
        {"data": {"user": {
            "pk": pk, "username": username, "full_name": "Bet Shop",
            "biography": "scrivi info@bet.it", "follower_count": 100,
            "following_count": 5, "is_private": False, "is_verified": False,
            "external_url": "", "bio_links": [],
        }}},
    )


def _wpi(username, uid="1"):
    return FakeResponse(
        f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}",
        200,
        {"data": {"user": {
            "id": uid, "username": username, "biography": "x",
            "edge_followed_by": {"count": 7}, "edge_follow": {"count": 2},
        }}},
    )


@pytest.fixture(autouse=True)
def _azzera_contatore():
    browser_bio.reset_contatore_ripieghi()
    yield


@pytest.mark.asyncio
async def test_graphql_in_ritardo_viene_usato_senza_nessuna_fetch():
    """Il cuore dell'inversione: il GraphQL arriva 0,1s dopo il goto e la finestra
    di attesa e' larga (5s). Il codice deve uscire APPENA arriva, senza chiedere
    niente e senza consumare il timeout."""
    page = FakePageRitardata(
        responses=[(0.1, _gql("betshop"))],
        evaluate_result={"__status": 400},
    )
    t0 = time.monotonic()
    user = await _capture_web_profile_info(page, "betshop", timeout_s=5.0)
    elapsed = time.monotonic() - t0

    assert user is not None
    assert user["id"] == "999", "deve venire dal GraphQL, normalizzato"
    assert page.evaluate_calls == [], "nessuna richiesta esplicita: e' il punto del passo 4"
    assert browser_bio.contatore_ripieghi() == 0
    # Non ha aspettato la finestra intera: prova che l'attesa e' guidata dall'arrivo.
    assert elapsed < 2.0, f"ha consumato {elapsed:.1f}s della finestra da 5s"


@pytest.mark.asyncio
async def test_web_profile_info_passivo_vince_se_arriva_anche_lui():
    """`web_profile_info` passivo costa zero come il GraphQL ed e' piu' ricco
    (espone `is_professional_account` in modo affidabile, il GraphQL no). Se
    arriva, si preferisce quello. Nessuna fetch in nessuno dei due casi."""
    page = FakePageRitardata(
        responses=[(0.05, _wpi("betshop")), (0.1, _gql("betshop"))],
        evaluate_result={"__status": 400},
    )
    user = await _capture_web_profile_info(page, "betshop", timeout_s=5.0)
    assert user["id"] == "1", "dal web_profile_info passivo, non dal GraphQL (999)"
    assert page.evaluate_calls == []


@pytest.mark.asyncio
async def test_nessuna_sorgente_passiva_allora_fetch_esplicita_e_contatore_sale():
    """Il ripiego resta, ma va CONTATO: senza contatore una regressione futura
    tornerebbe a una richiesta per profilo senza che nessuno se ne accorga."""
    page = FakePageRitardata(
        responses=[],
        evaluate_result={"data": {"user": {
            "id": "42", "username": "betshop", "biography": "y",
            "edge_followed_by": {"count": 1}, "edge_follow": {"count": 1},
        }}},
    )
    user = await _capture_web_profile_info(page, "betshop", timeout_s=0.3)
    assert user["id"] == "42"
    assert len(page.evaluate_calls) == 1
    assert browser_bio.contatore_ripieghi() == 1


@pytest.mark.asyncio
async def test_graphql_di_un_altro_profilo_non_fa_uscire_dall_attesa():
    """`/api/graphql` serve molte query diverse. Il payload di un altro username
    non e' una sorgente valida: non deve chiudere l'attesa ne' essere usato."""
    page = FakePageRitardata(
        responses=[(0.05, _gql("qualcunaltro"))],
        evaluate_result={"__status": 400},
    )
    user = await _capture_web_profile_info(page, "betshop", timeout_s=0.3)
    assert user == {"__status": 400}, "nessun recupero: torna il fail della fetch"
    assert len(page.evaluate_calls) == 1
    assert browser_bio.contatore_ripieghi() == 1


@pytest.mark.asyncio
async def test_interstiziale_esce_prima_di_qualunque_richiesta():
    """Insistere da dietro un avviso di blocco aggiunge richieste attribuibili nel
    momento peggiore. Si esce subito, senza attendere e senza chiedere."""
    page = FakePageRitardata(
        responses=[(0.05, _gql("betshop"))],
        evaluate_result={"__status": 400},
        redirect_to=BLOCK_URL,
    )
    user = await _capture_web_profile_info(page, "betshop", timeout_s=5.0)
    assert user.get("__blocked"), f"atteso blocco, ottenuto {user!r}"
    assert page.evaluate_calls == []
    assert browser_bio.contatore_ripieghi() == 0


@pytest.mark.asyncio
async def test_rate_limit_non_mascherato_dal_graphql_arrivato_dopo():
    """429 sulla fetch = soft-block reale: va propagato. Mascherarlo con un
    GraphQL arrivato nel frattempo farebbe martellare l'account alla cieca."""
    page = FakePageRitardata(
        responses=[(0.5, _gql("betshop"))],   # arriva DOPO la fine dell'attesa
        evaluate_result={"__status": 429},
    )
    user = await _capture_web_profile_info(page, "betshop", timeout_s=0.2)
    assert user == {"__status": 429}


@pytest.mark.asyncio
async def test_dati_passivi_usati_anche_con_un_429_su_unaltra_query():
    """Regola di priorita', decisa esplicitamente: i dati passivi vincono sul 429
    passivo. Sono gia' in mano e non sono costati richieste; buttarli perche' IG ha
    throttlato un'altra query non protegge nulla e perde il lead. Il 429 serve a
    decidere di NON chiedere, e quel caso e' coperto dal test successivo."""
    rate_limit = FakeResponse("https://www.instagram.com/api/graphql", 429, {})
    page = FakePageRitardata(
        responses=[(0.05, _gql("betshop")), (0.05, rate_limit)],
        evaluate_result={"__status": 400},
    )
    user = await _capture_web_profile_info(page, "betshop", timeout_s=1.0)
    assert user["id"] == "999", f"attesi i dati GraphQL, ottenuto {user!r}"
    assert page.evaluate_calls == []


@pytest.mark.asyncio
async def test_429_passivo_senza_dati_impedisce_la_fetch():
    """Il caso che conta: nessun dato passivo E un throttle visto. Insistere con una
    richiesta esplicita da dentro un rate-limit e' il modo di trasformarlo in blocco.
    Si esce col soft-block, senza chiedere e senza contare un ripiego."""
    rate_limit = FakeResponse(
        "https://www.instagram.com/api/v1/users/web_profile_info/?username=betshop", 429, {}
    )
    page = FakePageRitardata(responses=[(0.1, rate_limit)], evaluate_result=None)
    t0 = time.monotonic()
    user = await _capture_web_profile_info(page, "betshop", timeout_s=5.0)
    elapsed = time.monotonic() - t0
    assert user == {"__status": 429}
    assert elapsed < 2.0, f"ha consumato {elapsed:.1f}s della finestra da 5s"
    assert browser_bio.contatore_ripieghi() == 0
