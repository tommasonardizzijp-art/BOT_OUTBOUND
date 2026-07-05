from app.services.profile_lookup import pick_from_module


def test_from_module_is_never_self_profile():
    # Su migliaia di lookup di ALTRI profili, "self_profile" non deve mai comparire
    # (self_profile = stai guardando il TUO profilo = firma bot su profili altrui).
    vals = {pick_from_module() for _ in range(500)}
    assert "self_profile" not in vals


def test_from_module_only_valid_instagrapi_values():
    # instagrapi assert-a from_module in INFO_FROM_MODULES (self_profile|feed_timeline|reel_feed_timeline).
    allowed = {"feed_timeline", "reel_feed_timeline"}
    vals = {pick_from_module() for _ in range(500)}
    assert vals.issubset(allowed)


def test_from_module_varies():
    vals = {pick_from_module() for _ in range(500)}
    assert len(vals) >= 2  # non un singolo valore fisso
