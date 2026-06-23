"""Regression test: build_inbox_source must return a 4-tuple (source, own_pk, account, cleanup)
so that run_inbox_list can bind `account` before the challenge-isolation guard fires.

Locks the fix for: https://github.com/... (inbox challenge isolation bug)
"""
import types

import pytest


# ── Minimal fake objects ────────────────────────────────────────────────────


class _FakeCampaign:
    """Minimal campaign object with attributes needed by build_inbox_source."""
    def __init__(self):
        self.inbox_engine = "api"
        self.id = "test-campaign-x"
        self.scrape_cursor = None


class _FakeClient:
    """Minimal instagrapi-like client returned by the patched _login."""
    user_id = "12345"


_SENTINEL_ACCOUNT = object()  # unique sentinel — identity check in assertion


# ── Regression test ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_inbox_source_returns_4tuple_with_account(monkeypatch):
    """build_inbox_source must return (source, own_pk, account, cleanup) for engine='api'.

    The account element must be the exact object returned by _single_inbox_account,
    so run_inbox_list can bind it before the challenge guard executes.
    """
    import app.services.scrape_inbox as module

    # Patch _login to return a fake client with a known user_id
    async def _fake_login(account, db):
        return _FakeClient()

    # Patch _single_inbox_account to return our sentinel
    async def _fake_single_inbox_account(db, campaign_id):
        return _SENTINEL_ACCOUNT

    monkeypatch.setattr(module, "_login", _fake_login)
    monkeypatch.setattr(module, "_single_inbox_account", _fake_single_inbox_account)

    campaign = _FakeCampaign()
    result = await module.build_inbox_source(db=None, campaign=campaign)

    # Must be a 4-tuple
    assert len(result) == 4, (
        f"build_inbox_source returned {len(result)}-tuple, expected 4. "
        "The account element is missing — challenge isolation guard will never fire."
    )

    source, own_pk, account, cleanup = result

    # The 3rd element must be the exact account returned by _single_inbox_account
    assert account is _SENTINEL_ACCOUNT, (
        f"account element is {account!r}, expected the sentinel account object. "
        "Challenge isolation guard in run_inbox_list would see None instead of the real account."
    )

    # own_pk must be the integer conversion of client.user_id
    assert own_pk == 12345

    # cleanup must be awaitable
    await cleanup()
