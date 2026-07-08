"""Arricchimento contatti del motore browser via /api/v1/users/{pk}/info/.
web_profile_info non espone email/telefono business (business_email=null); il fetch
in-page a /info/ (app-id web) li porta in public_email/public_phone. Questi test
coprono l'helper e il fatto che il merge fa uscire l'email da extract_contacts."""
import pytest

from app.services.browser_bio import _fetch_public_contact_inpage, web_user_to_shim
from app.utils.contact_extract import extract_contacts


class _FakePage:
    def __init__(self, result):
        self._result = result
        self.calls = 0
        self.last_args = None

    async def evaluate(self, js, args):
        self.calls += 1
        self.last_args = args
        return self._result


@pytest.mark.asyncio
async def test_fetch_contact_success_returns_fields():
    page = _FakePage({
        "public_email": "shop@example.it",
        "public_phone_number": "0636001893",
        "contact_phone_number": "0636001893",
        "public_phone_country_code": "+39",
    })
    out = await _fetch_public_contact_inpage(page, "12345")
    assert out["public_email"] == "shop@example.it"
    assert out["public_phone_number"] == "0636001893"
    # pk deve arrivare come stringa nel fetch JS
    assert page.last_args[0] == "12345"


@pytest.mark.asyncio
async def test_fetch_contact_rate_limited_returns_marker():
    # 429/401/403 su /info/ NON va ingoiato: torna il marker rate-limited (Fix B),
    # cosi' il chiamante lo propaga come soft_block invece di continuare cieco.
    for st in (429, 401, 403):
        page = _FakePage({"__status": st})
        out = await _fetch_public_contact_inpage(page, "1")
        assert out == {"__rate_limited": st}


@pytest.mark.asyncio
async def test_fetch_contact_other_status_returns_none():
    # status non-rate-limit (es. 404) = miss benigno -> None (nessun soft_block)
    page = _FakePage({"__status": 404})
    assert await _fetch_public_contact_inpage(page, "1") is None


@pytest.mark.asyncio
async def test_fetch_contact_js_error_returns_none():
    page = _FakePage({"__err": "TypeError"})
    assert await _fetch_public_contact_inpage(page, "1") is None


@pytest.mark.asyncio
async def test_fetch_contact_no_pk_skips_call():
    page = _FakePage({"public_email": "x@y.it"})
    assert await _fetch_public_contact_inpage(page, None) is None
    assert page.calls == 0  # nessuna chiamata se manca il pk


def test_merge_recovers_email_hidden_in_business_field():
    """web_profile_info da' business_email=null; dopo il merge di /info/ (public_email),
    extract_contacts deve far uscire l'email — il caso reale dei profili business."""
    user = {
        "id": "999", "username": "shop", "full_name": "Shop", "biography": "solo testo, nessuna email",
        "business_email": None, "business_phone_number": None, "external_url": None,
        "edge_followed_by": {"count": 100}, "edge_follow": {"count": 50}, "bio_links": [],
    }
    shim = web_user_to_shim(user)
    assert extract_contacts(shim).email is None  # prima del merge: niente email

    # merge come in fetch_and_store_bio_browser
    info = {"public_email": "shop@example.it", "public_phone_number": "0636001893",
            "contact_phone_number": "0636001893", "public_phone_country_code": None}
    shim.public_email = info["public_email"] or shim.public_email
    shim.public_phone_number = info["public_phone_number"] or shim.public_phone_number
    shim.contact_phone_number = info["contact_phone_number"] or shim.contact_phone_number

    c = extract_contacts(shim)
    assert c.email == "shop@example.it"
    assert c.phone is not None
