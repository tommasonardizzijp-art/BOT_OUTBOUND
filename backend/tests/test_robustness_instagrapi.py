"""
End-to-end robustness tests for instagrapi-related failure modes.

Covers three independent fix layers applied 2026-06-06:

  1. MediaXma patch   — video_url=null from IG no longer crashes direct_threads()
  2. GQL verify 429   — IP-level rate limit on web_profile_info does not block login
  3. Recovery dedup   — dm_recovery_instagrapi_error fires max 1 alert per 30 min
  4. _InstagrapiParseError — parse error leaves message as 'sending', not 'error'
  5. reply_checker    — ValidationError on direct_threads → return 0, no crash
"""

import asyncio
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest


# ─────────────────────────────────────────────────────────
# 1. MediaXma patch
# ─────────────────────────────────────────────────────────

class TestMediaXmaPatch:
    """The monkey-patch must make MediaXma accept video_url=None and rebuild
    DirectMessage/ReplyMessage validators accordingly."""

    def test_media_xma_accepts_none_video_url(self):
        # Importing instagrapi_client triggers _patch_media_xma()
        import app.utils.instagrapi_client  # noqa: F401
        import instagrapi.types as t

        xma = t.MediaXma(video_url=None, title="reel senza url")
        assert xma.video_url is None

    def test_media_xma_still_accepts_valid_url(self):
        import app.utils.instagrapi_client  # noqa: F401
        import instagrapi.types as t

        xma = t.MediaXma(video_url="https://example.com/video.mp4", title="reel ok")
        assert xma.video_url is not None

    def test_direct_message_xma_share_accepts_null_video_url(self):
        """DirectMessage.xma_share field must tolerate video_url=null inside its dict."""
        import app.utils.instagrapi_client  # noqa: F401
        import instagrapi.types as t

        dm = t.DirectMessage(
            id="1",
            user_id="42",
            timestamp=1_000_000,
            item_type="xma_link",
            xma_share={"video_url": None, "title": "reel condiviso"},
        )
        assert dm.xma_share is not None
        assert dm.xma_share.video_url is None

    def test_patch_is_idempotent(self):
        """Calling _patch_media_xma() twice must not raise."""
        from app.utils.instagrapi_client import _patch_media_xma
        _patch_media_xma()
        _patch_media_xma()  # second call: field already optional → return early

    def test_extractor_built_xma_instance_validates(self):
        """Regression: instagrapi's extractor builds a MediaXma INSTANCE via the
        reference it captured at import time (instagrapi.extractors.MediaXma), then
        assigns it to DirectMessage.xma_share. The patch must keep class identity so
        that instance still validates — a subclass-based patch breaks this with
        'Input should be a valid dictionary or instance of _PatchedMediaXma'.
        """
        import app.utils.instagrapi_client  # noqa: F401 — triggers patch
        import instagrapi.types as t
        import instagrapi.extractors as ex

        # The extractor's MediaXma must be the SAME object the model validates against.
        assert ex.MediaXma is t.MediaXma

        # Build an instance the way the extractor does (real HttpUrl video_url set).
        xma_instance = ex.MediaXma(video_url="https://example.com/v.mp4", title="reel")
        dm = t.DirectMessage(
            id="1", user_id="42", timestamp=1_000_000,
            item_type="xma_share", xma_share=xma_instance,
        )
        assert dm.xma_share is not None
        assert dm.xma_share.video_url is not None


# ─────────────────────────────────────────────────────────
# 2. GQL verify 429 bypass
# ─────────────────────────────────────────────────────────

class TestGqlVerify429Bypass:
    """_do_login must skip GQL verify gracefully when Instagram returns 429
    on the web_profile_info endpoint (IP-level rate limit)."""

    def _fake_account(self) -> MagicMock:
        acc = MagicMock()
        acc.id = "acct-test"
        acc.username = "test_account"
        acc.proxy = None
        acc.session_data = json.dumps({"user_agent": "Mozilla/5.0", "authorization_data": {}, "cookies": {}})
        acc.last_login_at = None
        return acc

    def _fake_db(self) -> AsyncMock:
        db = AsyncMock()
        db.commit = AsyncMock()
        return db

    @pytest.mark.asyncio
    async def test_login_succeeds_when_gql_returns_429(self):
        from app.utils.instagrapi_client import _do_login

        exc_429 = Exception("RetryError: too many 429 error responses")
        with patch("instagrapi.Client") as MockClient:
            client = MagicMock()
            client.user_id = "111"
            client.get_settings.return_value = {}
            client.user_info_by_username_gql.side_effect = exc_429
            MockClient.return_value = client

            result = await _do_login(self._fake_account(), self._fake_db(), skip_gql_verify=False)
            assert result.user_id == "111"

    @pytest.mark.asyncio
    async def test_login_succeeds_when_gql_returns_retryerror(self):
        from app.utils.instagrapi_client import _do_login

        exc_retry = Exception("RetryError: HTTPSConnectionPool Max retries exceeded (Caused by ResponseError(too many 429))")
        with patch("instagrapi.Client") as MockClient:
            client = MagicMock()
            client.user_id = "222"
            client.get_settings.return_value = {}
            client.user_info_by_username_gql.side_effect = exc_retry
            MockClient.return_value = client

            result = await _do_login(self._fake_account(), self._fake_db(), skip_gql_verify=False)
            assert result.user_id == "222"

    @pytest.mark.asyncio
    async def test_login_fails_on_challenge_error(self):
        """Non-429 errors (challenge, auth) must still propagate and block login."""
        from app.utils.instagrapi_client import _do_login
        from app.utils.exceptions import AccountChallengeError, ScraperError

        exc_challenge = Exception("ChallengeRequired: checkpoint_required")
        with patch("instagrapi.Client") as MockClient:
            client = MagicMock()
            client.user_id = "333"
            client.get_settings.return_value = {}
            client.user_info_by_username_gql.side_effect = exc_challenge
            MockClient.return_value = client

            with pytest.raises((AccountChallengeError, ScraperError)):
                await _do_login(self._fake_account(), self._fake_db(), skip_gql_verify=False)

    @pytest.mark.asyncio
    async def test_login_fails_on_session_expired(self):
        """'LoginRequired' is NOT a 429 — must still block login."""
        from app.utils.instagrapi_client import _do_login
        from app.utils.exceptions import ScraperError

        exc_login = Exception("LoginRequired: session expired")
        with patch("instagrapi.Client") as MockClient:
            client = MagicMock()
            client.user_id = "444"
            client.get_settings.return_value = {}
            client.user_info_by_username_gql.side_effect = exc_login
            MockClient.return_value = client

            with pytest.raises(ScraperError):
                await _do_login(self._fake_account(), self._fake_db(), skip_gql_verify=False)

    @pytest.mark.asyncio
    async def test_skip_gql_verify_flag_still_works(self):
        """skip_gql_verify=True must never call user_info_by_username_gql."""
        from app.utils.instagrapi_client import _do_login

        with patch("instagrapi.Client") as MockClient:
            client = MagicMock()
            client.user_id = "555"
            client.get_settings.return_value = {}
            MockClient.return_value = client

            result = await _do_login(self._fake_account(), self._fake_db(), skip_gql_verify=True)
            client.user_info_by_username_gql.assert_not_called()
            assert result.user_id == "555"


# ─────────────────────────────────────────────────────────
# 3. Recovery checker anomaly dedup
# ─────────────────────────────────────────────────────────

class TestRecoveryAnomalyDedup:
    """_should_report_anomaly must rate-limit anomaly reports per account+kind."""

    def setup_method(self):
        # Reset the module-level dict before each test
        from app.services import recovery_checker
        recovery_checker._anomaly_last_reported.clear()

    def test_first_call_returns_true(self):
        from app.services.recovery_checker import _should_report_anomaly
        assert _should_report_anomaly("acct-1", "dm_recovery_instagrapi_error") is True

    def test_second_immediate_call_returns_false(self):
        from app.services.recovery_checker import _should_report_anomaly
        _should_report_anomaly("acct-1", "dm_recovery_instagrapi_error")
        assert _should_report_anomaly("acct-1", "dm_recovery_instagrapi_error") is False

    def test_dedup_is_per_account(self):
        """Different accounts are tracked separately."""
        from app.services.recovery_checker import _should_report_anomaly
        _should_report_anomaly("acct-1", "dm_recovery_instagrapi_error")
        # Different account → first occurrence → True
        assert _should_report_anomaly("acct-2", "dm_recovery_instagrapi_error") is True

    def test_dedup_is_per_kind(self):
        """Different error kinds are tracked separately for the same account."""
        from app.services.recovery_checker import _should_report_anomaly
        _should_report_anomaly("acct-1", "dm_recovery_instagrapi_error")
        # Same account, different kind → True
        assert _should_report_anomaly("acct-1", "dm_recovery_no_evidence") is True

    def test_dedup_expires_after_window(self):
        """After 30 minutes the same account+kind is allowed again."""
        from app.services import recovery_checker
        from app.services.recovery_checker import _should_report_anomaly, _ANOMALY_DEDUP_SECONDS

        # Seed a stale entry (older than the dedup window)
        recovery_checker._anomaly_last_reported[("acct-1", "dm_recovery_instagrapi_error")] = (
            datetime.utcnow() - timedelta(seconds=_ANOMALY_DEDUP_SECONDS + 1)
        )
        assert _should_report_anomaly("acct-1", "dm_recovery_instagrapi_error") is True

    def test_eight_messages_same_account_produce_one_report(self):
        """Simulates 8 stale messages from same account — only first triggers report."""
        from app.services.recovery_checker import _should_report_anomaly
        account_id = "acct-primero"
        results = [
            _should_report_anomaly(account_id, "dm_recovery_instagrapi_error")
            for _ in range(8)
        ]
        assert results.count(True) == 1
        assert results[0] is True
        assert all(r is False for r in results[1:])

    def test_none_account_id_dedups_correctly(self):
        """account_id=None uses empty string key — still deduplicates."""
        from app.services.recovery_checker import _should_report_anomaly
        assert _should_report_anomaly(None, "dm_recovery_instagrapi_error") is True
        assert _should_report_anomaly(None, "dm_recovery_instagrapi_error") is False


# ─────────────────────────────────────────────────────────
# 4. recovery_checker: nessuna lettura API (verifica di consegna rimossa)
# ─────────────────────────────────────────────────────────

class TestRecoveryNoApiRead:
    """Il recovery NON deve piu' avere il path di lettura API (`_check_dm_delivered`,
    `direct_threads`, `_InstagrapiParseError`): era il pattern-API-nudo che fa
    scattare i checkpoint. Guardia di regressione."""

    def test_api_delivery_check_removed(self):
        import app.services.recovery_checker as rc
        assert not hasattr(rc, "_check_dm_delivered"), "la verifica di consegna via API deve essere rimossa"
        assert not hasattr(rc, "_InstagrapiParseError")

    def test_no_instagrapi_login_import(self):
        import app.services.recovery_checker as rc
        # _login (instagrapi) non deve piu' essere importato nel modulo
        assert not hasattr(rc, "_login"), "recovery_checker non deve importare il login instagrapi"


# ─────────────────────────────────────────────────────────
# 5. reply_checker robustness
# ─────────────────────────────────────────────────────────

class TestReplyCheckerRobustness:
    """_scan_inbox must return 0 and not crash when direct_threads raises
    pydantic.ValidationError (MediaXma or similar parse error)."""

    @pytest.mark.asyncio
    async def test_scan_inbox_returns_zero_on_validation_error(self):
        from app.services.reply_checker import _scan_inbox
        from pydantic import ValidationError, BaseModel, HttpUrl

        # Build a pydantic ValidationError
        class _Strict(BaseModel):
            url: HttpUrl
        try:
            _Strict(url=None)
        except ValidationError as ve:
            pydantic_exc = ve

        fake_account = MagicMock()
        fake_account.username = "test_account"
        fake_account.proxy = None
        fake_db = AsyncMock()
        sent_followers = {"12345": (MagicMock(), None)}

        with patch("app.services.reply_checker._login") as mock_login, \
             patch("asyncio.to_thread") as mock_to_thread:
            mock_client = MagicMock()
            mock_client.user_id = "99999"
            mock_login.return_value = mock_client
            mock_to_thread.side_effect = pydantic_exc

            result = await _scan_inbox(fake_account, sent_followers, fake_db)
            assert result == 0

    @pytest.mark.asyncio
    async def test_scan_inbox_propagates_non_parse_errors(self):
        """Auth/session errors must still propagate so the caller can log them."""
        from app.services.reply_checker import _scan_inbox

        fake_account = MagicMock()
        fake_account.username = "test_account"
        fake_db = AsyncMock()

        with patch("app.services.reply_checker._login") as mock_login:
            mock_login.side_effect = RuntimeError("session expired")

            with pytest.raises(RuntimeError):
                await _scan_inbox(fake_account, {}, fake_db)

    @pytest.mark.asyncio
    async def test_scan_inbox_handles_pending_inbox_parse_error_gracefully(self):
        """Even if direct_pending_inbox raises ValidationError, main threads are still scanned."""
        from app.services.reply_checker import _scan_inbox
        from pydantic import ValidationError, BaseModel, HttpUrl

        class _Strict(BaseModel):
            url: HttpUrl
        try:
            _Strict(url=None)
        except ValidationError as ve:
            pydantic_exc = ve

        fake_account = MagicMock()
        fake_account.username = "test_account"
        fake_account.proxy = None
        fake_db = AsyncMock()

        # direct_threads returns empty list (no replies), direct_pending_inbox raises
        call_count = 0
        def _side_effect(fn, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return []  # direct_threads → empty
            raise pydantic_exc  # direct_pending_inbox → parse error

        with patch("app.services.reply_checker._login") as mock_login, \
             patch("asyncio.to_thread", side_effect=_side_effect):
            mock_client = MagicMock()
            mock_client.user_id = "99999"
            mock_login.return_value = mock_client

            result = await _scan_inbox(fake_account, {}, fake_db)
            assert result == 0  # no crash, returns 0
