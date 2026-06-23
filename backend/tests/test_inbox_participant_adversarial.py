"""Adversarial tests for extract_thread_participant.

Goal: find inputs that cause crashes or wrong/surprising results.
The function must never raise an unhandled exception; it should return
sensible (None vs tuple) values for hostile inputs.

Defects are documented inline with DEFECT comments.
"""
from types import SimpleNamespace as NS
import pytest
from app.services.inbox_source import extract_thread_participant

OWN = 999


def _u(pk, username):
    return NS(pk=pk, username=username)


# ---------------------------------------------------------------------------
# .pk edge cases
# ---------------------------------------------------------------------------

class NoPkAttr:
    """Object with NO .pk attribute at all."""
    def __init__(self, username):
        self.username = username


def test_pk_missing_attribute_no_crash():
    """User object without a .pk attribute at all — must not raise AttributeError."""
    users = [NoPkAttr("mario")]
    # Should skip the user (AttributeError caught) → None (no valid others)
    result = extract_thread_participant(users, OWN)
    assert result is None


def test_pk_none_skipped():
    """pk=None → int(None) raises TypeError → user skipped → None."""
    assert extract_thread_participant([_u(None, "mario")], OWN) is None


def test_pk_float_coerced():
    """pk=123.0 → int(123.0) == 123 → valid; should return the user."""
    result = extract_thread_participant([_u(123.0, "mario")], OWN)
    assert result == (123, "mario")


def test_pk_bool_true_coerced():
    """pk=True → int(True) == 1 → treated as user id 1; not own_pk (999)."""
    result = extract_thread_participant([_u(True, "mario")], OWN)
    assert result == (1, "mario")


def test_pk_bool_false_skipped_if_own_pk_is_zero():
    """pk=False → int(False) == 0; if own_pk=0 it would be self-filtered."""
    # own_pk=0, user has pk=False → int(False)=0 → equals own_pk → skipped
    result = extract_thread_participant([_u(False, "mario")], 0)
    assert result is None


def test_pk_non_numeric_string_skipped():
    """pk='abc' → int('abc') raises ValueError → user skipped."""
    assert extract_thread_participant([_u("abc", "mario")], OWN) is None


def test_pk_empty_string_skipped():
    """pk='' → int('') raises ValueError → user skipped."""
    assert extract_thread_participant([_u("", "mario")], OWN) is None


def test_pk_huge_int():
    """pk= very large int — Python handles arbitrary precision; must not crash."""
    huge = 10 ** 50
    result = extract_thread_participant([_u(huge, "mario")], OWN)
    assert result == (huge, "mario")


def test_pk_string_with_spaces_coerced():
    """pk=' 123 ' → int(' 123 ') == 123 in Python (strips whitespace) → valid."""
    result = extract_thread_participant([_u(" 123 ", "mario")], OWN)
    assert result == (123, "mario")


# ---------------------------------------------------------------------------
# username edge cases
# ---------------------------------------------------------------------------

def test_username_none_skipped():
    """username=None → `not None` is True → returns None (already tested, baseline)."""
    assert extract_thread_participant([_u(123, None)], OWN) is None


def test_username_empty_string_skipped():
    """username='' → `not ''` is True → returns None."""
    assert extract_thread_participant([_u(123, "")], OWN) is None


def test_username_whitespace_only():
    """username='  ' → `not '  '` is FALSE in Python → function returns (123, '  ').

    DEFECT: whitespace-only username passes the `if not username` guard and is
    returned as a valid participant. Callers using the username for display or
    as a lookup key will silently get garbage.
    """
    result = extract_thread_participant([_u(123, "  ")], OWN)
    # CORRECT robust behavior: should be None (invalid username)
    # ACTUAL behavior: (123, '  ') — defect
    assert result is None, (
        f"DEFECT: whitespace-only username slipped through; got {result!r}"
    )


def test_username_leading_trailing_spaces():
    """username=' mario ' — spaces are NOT stripped; caller receives them.

    This may or may not be a defect depending on contract. We assert the actual
    value is returned as-is (no strip) so the caller knows what to expect.
    """
    result = extract_thread_participant([_u(123, " mario ")], OWN)
    # Not a crash; but note caller gets un-stripped username
    assert result == (123, " mario ")


def test_username_at_prefix():
    """username='@mario' — valid non-empty string; function returns it."""
    result = extract_thread_participant([_u(123, "@mario")], OWN)
    assert result == (123, "@mario")


def test_username_non_string_int():
    """username=123 (int) → `not 123` is False → function returns (pk, 123).

    DEFECT: return type annotation promises str but actual value is int.
    Callers doing string operations (e.g., .lower(), .startswith()) will crash.
    """
    result = extract_thread_participant([_u(123, 123)], OWN)
    # CORRECT robust behavior: should be None (not a valid string username)
    # ACTUAL behavior: (123, 123) — type defect
    assert result is None, (
        f"DEFECT: non-string username (int) slipped through; got {result!r}"
    )


def test_username_non_string_list():
    """username=[] — falsy in Python, so `not []` is True → returns None (safe)."""
    assert extract_thread_participant([_u(123, [])], OWN) is None


def test_username_non_string_dict():
    """username={'a': 1} — truthy → `not {...}` is False → returns (pk, {'a':1}).

    DEFECT: same as int case — non-string passes the guard.
    """
    result = extract_thread_participant([_u(123, {"a": 1})], OWN)
    assert result is None, (
        f"DEFECT: non-string username (dict) slipped through; got {result!r}"
    )


# ---------------------------------------------------------------------------
# thread_users shape edge cases
# ---------------------------------------------------------------------------

def test_thread_users_none():
    """`thread_users=None` → `None or []` → empty list → returns None."""
    assert extract_thread_participant(None, OWN) is None


def test_thread_users_empty_list():
    assert extract_thread_participant([], OWN) is None


def test_thread_users_contains_none_entry():
    """A None entry in the list → `int(None.pk)` would raise AttributeError.

    The outer try/except catches AttributeError on `u.pk`, but `None` has no
    `.pk` attr — it fires `AttributeError: 'NoneType' object has no attribute 'pk'`.
    That IS caught. So this must not crash and must skip the None entry.
    """
    users = [None, _u(123, "mario")]
    result = extract_thread_participant(users, OWN)
    assert result == (123, "mario")


def test_thread_users_duplicate_users():
    """Two identical non-self users → len(others) == 2 → treated as group → None.

    Surprising: it's a 1-to-1 DM but duplicated data looks like a group.
    Document expected behavior: None (conservative).
    """
    users = [_u(123, "mario"), _u(123, "mario")]
    result = extract_thread_participant(users, OWN)
    assert result is None


def test_thread_users_all_invalid_pks():
    """All users have un-coercible pks → others is empty → None."""
    users = [_u("bad", "mario"), _u(None, "lucia"), NoPkAttr("third")]
    assert extract_thread_participant(users, OWN) is None


# ---------------------------------------------------------------------------
# own_pk type mismatch — self-filter correctness
# ---------------------------------------------------------------------------

def test_own_pk_as_string_self_filter_fails():
    """own_pk passed as str '999' instead of int 999.

    int(u.pk) produces int; comparing int(999) == '999' is always False in Python.
    The self-user is NOT filtered out and appears as an 'other' user.

    DEFECT: if own_pk is accidentally passed as str, the bot will treat itself
    as the conversation partner and return its own profile as the target contact.
    This is the most dangerous defect for a scraping run — silently corrupts data.
    """
    # Simulates: own account in thread_users, own_pk passed as str by caller
    users = [_u(OWN, "me")]  # only self in thread
    result = extract_thread_participant(users, str(OWN))
    # CORRECT behavior: own user should be filtered → None
    # ACTUAL behavior: own user NOT filtered → (999, 'me') — defect
    assert result is None, (
        f"DEFECT: own_pk=str failed to filter self; got {result!r}"
    )


def test_own_pk_as_string_in_real_1to1():
    """1-to-1 with own_pk as str: self present, other present.

    own_pk='999' str → coerced via `int(own_pk)` at entry → self at pk=999 IS
    filtered → exactly one other remains → returns (123, 'mario').

    Post-fix CORRECT behavior: own_pk coercion makes the str/int mismatch
    harmless; the real contact is returned, not lost.
    """
    users = [_u(OWN, "me"), _u(123, "mario")]
    result = extract_thread_participant(users, str(OWN))
    # own_pk coerced to int → self filtered → valid 1-to-1 contact returned
    assert result == (123, "mario")


def test_other_user_pk_str_999_vs_own_pk_int_999():
    """Other user has pk='999' (str), own_pk=999 (int).

    int('999') == 999 → True → correctly filtered as self. No defect here.
    """
    users = [_u("999", "mario")]
    result = extract_thread_participant(users, 999)
    assert result is None  # correctly identified as self


# ---------------------------------------------------------------------------
# Large input — must not hang or crash
# ---------------------------------------------------------------------------

def test_large_input_no_hang():
    """10000 users in thread_users — must complete quickly without crash."""
    import time
    users = [_u(i, f"user_{i}") for i in range(10000)]
    start = time.monotonic()
    result = extract_thread_participant(users, OWN)
    elapsed = time.monotonic() - start
    # 10000 users → group (>1 other after filtering own) → None
    assert result is None
    # Should complete in well under 1 second
    assert elapsed < 1.0, f"Too slow: {elapsed:.3f}s"


def test_large_input_single_valid_user():
    """10000 self-users + 1 other — correct extraction after large self-filtering."""
    users = [_u(OWN, "me")] * 10000 + [_u(123, "mario")]
    result = extract_thread_participant(users, OWN)
    # All OWN users filtered → others=[123] → should return (123, 'mario')
    assert result == (123, "mario")


# ---------------------------------------------------------------------------
# Boundary: own_pk matches across numeric types
# ---------------------------------------------------------------------------

def test_own_pk_zero():
    """own_pk=0 edge — user pk=0 should be filtered."""
    users = [_u(0, "zero_user")]
    assert extract_thread_participant(users, 0) is None


def test_own_pk_negative():
    """Negative own_pk — unusual but must not crash."""
    users = [_u(-1, "negative_user")]
    assert extract_thread_participant(users, -1) is None


def test_own_pk_float_not_equal_to_int():
    """own_pk=999.5 (float) passed in — int(u.pk)==999 != 999.5 → self not filtered.

    DEFECT: if caller passes a float own_pk, self-filtering breaks.
    The function signature says `own_pk: int` but Python doesn't enforce it.
    """
    users = [_u(OWN, "me")]  # pk=999, own_pk=999.5
    result = extract_thread_participant(users, 999.5)
    # int(999) != 999.5 → not filtered → (999, 'me') returned — defect
    assert result is None, (
        f"DEFECT: float own_pk failed to filter self; got {result!r}"
    )
