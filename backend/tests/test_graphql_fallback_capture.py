"""Integrazione: _capture_web_profile_info recupera da GraphQL passivo quando
web_profile_info fallisce col bug 400, senza fare NESSUNA nuova richiesta.
"""
import asyncio
import pytest

from app.services import browser_bio
from app.services.browser_bio import _capture_web_profile_info


class FakeResponse:
    def __init__(self, url, status, payload):
        self.url = url
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload


class FakePage:
    """Simula il minimo di Playwright usato da _capture_web_profile_info.

    - on/remove_listener: registra il callback 'response'.
    - goto: emette (await) le response programmate in `self.responses`.
    - evaluate: ritorna `self.evaluate_result` (simula il fetch in-page di
      web_profile_info: dict con __status per un HTTP fail, oppure il body).
    """
    def __init__(self, responses, evaluate_result):
        self.responses = responses
        self.evaluate_result = evaluate_result
        self._on_response = None
        self.evaluate_calls = []
        self.url = "https://www.instagram.com/"

    def on(self, event, cb):
        if event == "response":
            self._on_response = cb

    def remove_listener(self, event, cb):
        if event == "response" and self._on_response is cb:
            self._on_response = None

    async def goto(self, url, **kw):
        # Come il vero Playwright: dopo il goto raw_page.url riflette la pagina
        # navigata (letto da _capture_web_profile_info per il check di blocco).
        self.url = url
        for r in self.responses:
            if self._on_response is not None:
                await self._on_response(r)

    async def evaluate(self, script, args):
        self.evaluate_calls.append(args)
        return self.evaluate_result


def _gql_response(username):
    return FakeResponse(
        "https://www.instagram.com/api/graphql",
        200,
        {"data": {"user": {
            "pk": "999", "username": username, "full_name": "Bet Shop",
            "biography": "scrivi info@bet.it", "follower_count": 100,
            "following_count": 5, "is_private": False, "is_verified": False,
            "external_url": "", "bio_links": [],
        }}},
    )


@pytest.mark.asyncio
async def test_fallback_used_when_web_profile_info_400():
    # web_profile_info non intercettato passivamente + fetch in-page = 400 (bug asset).
    page = FakePage(
        responses=[_gql_response("betshop")],
        evaluate_result={"__status": 400},
    )
    user = await _capture_web_profile_info(page, "betshop", timeout_s=0.5)
    assert user is not None
    assert not user.get("__status")             # NON e' propagato come errore
    assert user["id"] == "999"                  # forma web_profile_info (normalizzata)
    assert user["edge_followed_by"]["count"] == 100
    assert len(page.evaluate_calls) == 1        # UN solo fetch (web_profile_info), NESSUN fetch GraphQL


@pytest.mark.asyncio
async def test_web_profile_info_success_ignores_graphql():
    # Passa una response web_profile_info 200: il primary path vince, GraphQL ignorato.
    wpi = FakeResponse(
        "https://www.instagram.com/api/v1/users/web_profile_info/?username=betshop",
        200,
        {"data": {"user": {"id": "1", "username": "betshop", "biography": "x",
                            "edge_followed_by": {"count": 7}, "edge_follow": {"count": 2}}}},
    )
    page = FakePage(responses=[wpi, _gql_response("betshop")], evaluate_result={"__status": 400})
    user = await _capture_web_profile_info(page, "betshop", timeout_s=0.5)
    assert user["id"] == "1"                    # dal web_profile_info, non dal GraphQL (999)
    assert len(page.evaluate_calls) == 0        # colto passivo: nessun fetch in-page


@pytest.mark.asyncio
async def test_rate_limit_not_masked_by_graphql():
    # 429 su web_profile_info: DEVE propagarsi come __status (soft_block), NON usare GraphQL.
    page = FakePage(responses=[_gql_response("betshop")], evaluate_result={"__status": 429})
    user = await _capture_web_profile_info(page, "betshop", timeout_s=0.5)
    assert user == {"__status": 429}


@pytest.mark.asyncio
async def test_graphql_of_wrong_user_ignored():
    # Una GraphQL per un ALTRO username non deve essere usata come fallback.
    page = FakePage(responses=[_gql_response("qualcunaltro")], evaluate_result={"__status": 400})
    user = await _capture_web_profile_info(page, "betshop", timeout_s=0.5)
    assert user == {"__status": 400}            # nessun recupero: torna il fail originale


@pytest.mark.asyncio
async def test_no_data_anywhere_returns_none():
    page = FakePage(responses=[], evaluate_result=None)
    user = await _capture_web_profile_info(page, "betshop", timeout_s=0.3)
    assert user is None
