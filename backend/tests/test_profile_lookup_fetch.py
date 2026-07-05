from unittest.mock import MagicMock

from app.services.profile_lookup import fetch_profile_app_like


def test_uses_app_like_from_module_not_self_profile():
    client = MagicMock()
    fake_user = object()
    client.user_info_v1.return_value = fake_user
    out = fetch_profile_app_like(client, "123")
    # Deve chiamare user_info_v1 con from_module realistico (non self_profile).
    _, kwargs = client.user_info_v1.call_args
    assert kwargs.get("from_module") in ("feed_timeline", "reel_feed_timeline")
    assert out is fake_user


def test_returns_user_even_if_post_fetch_fails():
    # Il fetch dei post e' best-effort: se solleva, la lookup deve comunque tornare l'user.
    client = MagicMock()
    fake_user = object()
    client.user_info_v1.return_value = fake_user
    client.user_medias_v1.side_effect = RuntimeError("boom")
    out = fetch_profile_app_like(client, "123")
    assert out is fake_user


def test_fetches_posts_like_the_app():
    # L'app all'apertura profilo carica anche la griglia post: deve chiamare user_medias_v1.
    client = MagicMock()
    client.user_info_v1.return_value = object()
    fetch_profile_app_like(client, "999")
    client.user_medias_v1.assert_called_once()
