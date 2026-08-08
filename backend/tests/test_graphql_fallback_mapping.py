"""GraphQL PolarisProfilePageContentQuery -> forma web_profile_info -> shim.

Verifica che il payload GraphQL (forma FLAT: pk/follower_count) venga normalizzato
nella forma web_profile_info (annidata: id/edge_followed_by.count) cosi' l'unico shim
`web_user_to_shim` produca gli stessi contatti del path API (anti-divergenza).
"""
from app.services.browser_bio import graphql_user_to_web_shape, web_user_to_shim
from app.utils.contact_extract import extract_contacts


def _sample_graphql_user() -> dict:
    # Forma reale osservata (audit 2026-07-29, profilo planetwinpiromallo).
    return {
        "pk": "77905145792",
        "username": "planetwinpiromallo",
        "full_name": "Planetwin Piromallo",
        "biography": "Via conte piromallo 40/42\nSan sebastiano al vesuvio scrivi info@pw.it",
        "follower_count": 658,
        "following_count": 92,
        "is_private": False,
        "is_verified": False,
        "external_url": "",
        "bio_links": [],
    }


def test_shape_normalizes_flat_counts_to_nested():
    web_shaped = graphql_user_to_web_shape(_sample_graphql_user())
    assert web_shaped["id"] == "77905145792"
    assert web_shaped["edge_followed_by"]["count"] == 658
    assert web_shaped["edge_follow"]["count"] == 92
    # I campi con nome gia' coincidente restano.
    assert web_shaped["username"] == "planetwinpiromallo"
    assert web_shaped["biography"].startswith("Via conte")


def test_shim_reads_counts_after_shape():
    shim = web_user_to_shim(graphql_user_to_web_shape(_sample_graphql_user()))
    assert shim.pk == "77905145792"
    assert shim.follower_count == 658      # sarebbe None senza la normalizzazione
    assert shim.following_count == 92
    assert shim.username == "planetwinpiromallo"


def test_contacts_extracted_end_to_end():
    shim = web_user_to_shim(graphql_user_to_web_shape(_sample_graphql_user()))
    c = extract_contacts(shim)
    # Email dal regex sulla bio (GraphQL non espone business_email, come web_profile_info).
    assert c.email == "info@pw.it"


def test_missing_and_empty_keys_are_safe():
    for g in ({}, {"username": "x"}, {"pk": None, "follower_count": None, "bio_links": None}):
        web_shaped = graphql_user_to_web_shape(g)
        shim = web_user_to_shim(web_shaped)
        c = extract_contacts(shim)
        assert c.email is None
        assert shim.follower_count is None


def test_id_falls_back_to_id_key_if_no_pk():
    # Difesa: se un giorno GraphQL usasse 'id' invece di 'pk', non perdiamo il pk.
    web_shaped = graphql_user_to_web_shape({"id": "42", "username": "z"})
    assert web_shaped["id"] == "42"


def test_graphql_shape_does_not_carry_media_count():
    # Task B.2 (review): il payload GraphQL reale osservato (audit 2026-07-29,
    # _sample_graphql_user sopra) non porta NESSUN campo media/post-count --
    # graphql_user_to_web_shape fa `dict(g)` puro (nessuna chiave da
    # normalizzare per questo campo, a differenza di follower/following), quindi
    # dopo lo shim il segnale primario di maybe_micro_scroll resta assente e si
    # ripiega sul DOM. Se IG dovesse iniziare a esporlo nella GraphQL, questo
    # test smette di essere vero e va aggiornato consapevolmente (non e' un
    #'deve restare cosi' per sempre', e' 'e' cosi' oggi, verificato').
    shim = web_user_to_shim(graphql_user_to_web_shape(_sample_graphql_user()))
    assert shim.media_count is None
