from types import SimpleNamespace

from app.models.message import MessageStatus
from app.services.campaign_orchestrator import _cancelled_attempt_is_safe_to_release


def _message(status: MessageStatus) -> SimpleNamespace:
    return SimpleNamespace(status=status)


def test_cancelled_attempt_release_only_before_possible_send():
    assert _cancelled_attempt_is_safe_to_release(None)
    assert _cancelled_attempt_is_safe_to_release(_message(MessageStatus.pending))
    assert _cancelled_attempt_is_safe_to_release(_message(MessageStatus.retry))

    assert not _cancelled_attempt_is_safe_to_release(_message(MessageStatus.sending))
    assert not _cancelled_attempt_is_safe_to_release(_message(MessageStatus.sent))
    assert not _cancelled_attempt_is_safe_to_release(_message(MessageStatus.failed))
