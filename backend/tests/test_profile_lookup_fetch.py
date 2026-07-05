from unittest.mock import MagicMock

from app.services.profile_lookup import fetch_profile_app_like
from app.config import settings


def test_default_from_module_is_self_profile_baseline():
    # DEFAULT (flag OFF): call IDENTICA alla baseline storica -> from_module="self_profile".
    # E' la chiamata che NON dava 429 immediato; il modulo realistico e' opt-in.
    assert settings.bio_realistic_from_module_enabled is False
    client = MagicMock()
    fake_user = object()
    client.user_info_v1.return_value = fake_user
    out = fetch_profile_app_like(client, "123")
    _, kwargs = client.user_info_v1.call_args
    assert kwargs.get("from_module") == "self_profile"
    assert out is fake_user


def test_realistic_from_module_when_flag_enabled(monkeypatch):
    # Opt-in: col flag ON usa un modulo realistico non-self (feed/reel).
    monkeypatch.setattr(settings, "bio_realistic_from_module_enabled", True)
    client = MagicMock()
    client.user_info_v1.return_value = object()
    fetch_profile_app_like(client, "123")
    _, kwargs = client.user_info_v1.call_args
    assert kwargs.get("from_module") in ("feed_timeline", "reel_feed_timeline")


def test_no_media_call_by_default():
    # DEFAULT: nessun user_medias_v1. Su sessione API nuda quella 2a chiamata
    # (endpoint /feed, rate-limit piu' duro, burst a gap zero) raddoppia il volume
    # e anticipa il 429. Il default DEVE restare OFF: questo test lo blocca.
    assert settings.bio_app_like_media_enabled is False
    client = MagicMock()
    client.user_info_v1.return_value = object()
    fetch_profile_app_like(client, "123")
    client.user_medias_v1.assert_not_called()


def test_fetches_posts_when_flag_enabled(monkeypatch):
    # Opt-in: col flag ON l'app-like torna a caricare la griglia post (una sola volta).
    monkeypatch.setattr(settings, "bio_app_like_media_enabled", True)
    client = MagicMock()
    client.user_info_v1.return_value = object()
    fetch_profile_app_like(client, "999")
    client.user_medias_v1.assert_called_once()


def test_returns_user_even_if_post_fetch_fails(monkeypatch):
    # Col fetch post attivo e fallante, la lookup deve comunque tornare l'user (best-effort).
    monkeypatch.setattr(settings, "bio_app_like_media_enabled", True)
    client = MagicMock()
    fake_user = object()
    client.user_info_v1.return_value = fake_user
    client.user_medias_v1.side_effect = RuntimeError("boom")
    out = fetch_profile_app_like(client, "123")
    assert out is fake_user
