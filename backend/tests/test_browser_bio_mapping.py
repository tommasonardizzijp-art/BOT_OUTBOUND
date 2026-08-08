"""Mapping puro web_profile_info -> shim -> extract_contacts (nessun IO/browser).

Garantisce che il percorso bio-via-browser produca gli stessi contatti del percorso
API (anti-divergenza): il JSON web viene mappato sugli stessi nomi-attributo che
extract_contacts si aspetta dall'oggetto instagrapi.
"""
from app.services.browser_bio import web_user_to_shim
from app.utils.contact_extract import extract_contacts


def _sample_business_user() -> dict:
    return {
        "id": "123456",
        "username": "shop_test",
        "full_name": "Shop Test",
        "biography": "Contattaci: info@shop.it tel +39 333 1234567 - wa.me/393331234567",
        "is_verified": False,
        "is_private": False,
        "external_url": "https://shop.it",
        "business_email": "biz@shop.it",
        "business_phone_number": "+393331234599",
        "edge_followed_by": {"count": 5000},
        "edge_follow": {"count": 300},
        "bio_links": [
            {"url": "https://linktr.ee/shop", "title": "Links", "link_type": "external"}
        ],
    }


def test_shim_maps_core_fields():
    shim = web_user_to_shim(_sample_business_user())
    assert shim.pk == "123456"
    assert shim.username == "shop_test"
    assert shim.full_name == "Shop Test"
    assert shim.follower_count == 5000
    assert shim.following_count == 300
    assert shim.external_url == "https://shop.it"
    assert shim.public_email == "biz@shop.it"
    assert shim.public_phone_number == "+393331234599"
    assert shim.bio_links == [{"url": "https://linktr.ee/shop", "title": "Links"}]


def test_business_contacts_extracted():
    shim = web_user_to_shim(_sample_business_user())
    c = extract_contacts(shim)
    # Email business ha priorita' sul regex della bio.
    assert c.email == "biz@shop.it"
    assert c.sources["email"] == "ig_business"
    # Telefono business valorizzato.
    assert c.phone and c.phone.endswith("393331234599")
    assert c.sources["phone"] == "ig_business"
    # WhatsApp dal wa.me nella bio.
    assert c.whatsapp and "393331234567" in c.whatsapp
    # external_url + linktr.ee tra i bio_links.
    urls = {l["url"] for l in c.bio_links}
    assert "https://shop.it" in urls
    assert "https://linktr.ee/shop" in urls


def test_regex_fallback_when_no_business_fields():
    # Nessun campo business: email/telefono devono venire dal regex sulla bio.
    user = {
        "id": "9",
        "username": "creator9",
        "full_name": "Creator",
        "biography": "scrivimi a hello@creator.io oppure +39 340 9998877",
        "edge_followed_by": {"count": 12},
        "edge_follow": {"count": 40},
    }
    c = extract_contacts(web_user_to_shim(user))
    assert c.email == "hello@creator.io"
    assert c.sources["email"] == "bio_regex"
    assert c.phone and c.phone.endswith("3409998877")
    assert c.sources["phone"] == "bio_regex"


def test_empty_and_missing_keys_are_safe():
    # Robustezza: dict vuoto o chiavi mancanti non devono sollevare.
    for u in ({}, {"username": "x"}, {"edge_followed_by": None, "bio_links": None}):
        shim = web_user_to_shim(u)
        c = extract_contacts(shim)
        assert c.email is None
        assert c.phone is None
        assert shim.follower_count is None


def test_shim_maps_media_count_when_present():
    # Task B.2 (review): web_profile_info nativo espone il conteggio post in
    # edge_owner_to_timeline_media.count -- segnale primario per maybe_micro_scroll.
    user = _sample_business_user()
    user["edge_owner_to_timeline_media"] = {"count": 7}
    shim = web_user_to_shim(user)
    assert shim.media_count == 7


def test_shim_media_count_none_when_absent():
    # Nessun edge_owner_to_timeline_media nel payload (caso comune, vedi
    # test_graphql_fallback_mapping.py per il ramo GraphQL): media_count deve
    # restare None, non 0 -- None e' "non disponibile" (ripiego DOM), 0 e'
    # "griglia vuota confermata" (nessuna attesa). Sono segnali diversi.
    shim = web_user_to_shim(_sample_business_user())
    assert shim.media_count is None
