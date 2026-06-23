"""Adversarial tests for parse_thread_rows — must NEVER raise.

Contract:
  parse_thread_rows(rows_data, own_pk) -> list[tuple[int, str]]
  - Returns only (int pk, non-blank str username) tuples.
  - Skips rows where pk cannot be converted to int.
  - Skips rows where pk == own_pk (self-filter), regardless of type coercion.
  - Skips rows where username is falsy (None / empty str).
  - Never raises — a crash aborts a live browser scraping run.

DEFECTS found during authoring (marked [DEFECT]):
  1. [DEFECT] Non-dict rows (None, str, int, object) crash with AttributeError
     because row.get() is called without a guard.
  2. [DEFECT] Non-string username (e.g. int 123) passes `not username` check
     and leaks into output as tuple[int, int], violating the (int, str) contract
     and crashing downstream callers that expect str.
  3. [DEFECT] Whitespace-only username (e.g. "   ") passes `not username` check
     (non-empty str is truthy) and leaks into output, violating the non-blank contract.
"""

import pytest
from app.services.inbox_browser_source import parse_thread_rows

OWN = 999


# ---------------------------------------------------------------------------
# rows_data shape
# ---------------------------------------------------------------------------

class TestRowsDataShape:
    def test_none_rows_data(self):
        """rows_data=None must return [] without raising."""
        assert parse_thread_rows(None, OWN) == []

    def test_empty_list(self):
        assert parse_thread_rows([], OWN) == []

    # [DEFECT] — the following two tests are expected to FAIL on the current
    # implementation because non-dict entries trigger AttributeError.
    # They are written to assert the desired ROBUST contract.

    def test_list_containing_none_entry(self):
        """A list with a None entry must skip it, not crash."""
        rows = [None, {"pk": 1, "username": "mario"}]
        # Should not raise; should skip None and return the valid row.
        result = parse_thread_rows(rows, OWN)
        assert result == [(1, "mario")]

    def test_list_containing_string_entry(self):
        """A list with a bare string entry must skip it, not crash."""
        rows = ["garbage", {"pk": 2, "username": "luigi"}]
        result = parse_thread_rows(rows, OWN)
        assert result == [(2, "luigi")]

    def test_list_containing_int_entry(self):
        """A list with a bare int entry must skip it, not crash."""
        rows = [42, {"pk": 3, "username": "peach"}]
        result = parse_thread_rows(rows, OWN)
        assert result == [(3, "peach")]

    def test_list_containing_object_entry(self):
        """A list with an arbitrary object (no .get()) must skip it, not crash."""
        class Opaque:
            pass

        rows = [Opaque(), {"pk": 4, "username": "toad"}]
        result = parse_thread_rows(rows, OWN)
        assert result == [(4, "toad")]


# ---------------------------------------------------------------------------
# Missing keys in otherwise-dict rows
# ---------------------------------------------------------------------------

class TestMissingKeys:
    def test_row_missing_pk_key(self):
        """Dict without 'pk' key: row.get('pk') returns None → int(None) raises
        TypeError → caught → row skipped."""
        rows = [{"username": "mario"}]
        result = parse_thread_rows(rows, OWN)
        assert result == []

    def test_row_missing_username_key(self):
        """Dict without 'username' key: row.get('username') returns None →
        falsy → row skipped."""
        rows = [{"pk": 5}]
        result = parse_thread_rows(rows, OWN)
        assert result == []

    def test_row_empty_dict(self):
        """Completely empty dict: both keys missing → skipped."""
        rows = [{}]
        result = parse_thread_rows(rows, OWN)
        assert result == []


# ---------------------------------------------------------------------------
# pk edge cases
# ---------------------------------------------------------------------------

class TestPkEdgeCases:
    def test_pk_none(self):
        rows = [{"pk": None, "username": "mario"}]
        assert parse_thread_rows(rows, OWN) == []

    def test_pk_empty_string(self):
        rows = [{"pk": "", "username": "mario"}]
        assert parse_thread_rows(rows, OWN) == []

    def test_pk_alphabetic_string(self):
        rows = [{"pk": "abc", "username": "mario"}]
        assert parse_thread_rows(rows, OWN) == []

    def test_pk_float_roundtrip(self):
        """pk=123.0 — int(123.0) = 123, valid."""
        rows = [{"pk": 123.0, "username": "mario"}]
        result = parse_thread_rows(rows, OWN)
        assert result == [(123, "mario")]

    def test_pk_float_fractional(self):
        """pk=123.7 — int(123.7) = 123 (truncated), accepted."""
        rows = [{"pk": 123.7, "username": "mario"}]
        result = parse_thread_rows(rows, OWN)
        # int() truncates, not rounds — implementation accepts this
        assert result == [(123, "mario")]

    def test_pk_true(self):
        """bool is subclass of int; True → int(True) = 1.
        This silently converts — not ideal but currently accepted."""
        rows = [{"pk": True, "username": "mario"}]
        result = parse_thread_rows(rows, OWN)
        # True converts to 1 — document actual behavior
        assert result == [(1, "mario")]

    def test_pk_false(self):
        """False → int(False) = 0; valid pk (not self if own_pk != 0)."""
        rows = [{"pk": False, "username": "mario"}]
        result = parse_thread_rows(rows, OWN)
        assert result == [(0, "mario")]

    def test_pk_str_with_spaces(self):
        """' 123 ' — int(' 123 ') = 123, Python strips whitespace."""
        rows = [{"pk": " 123 ", "username": "mario"}]
        result = parse_thread_rows(rows, OWN)
        assert result == [(123, "mario")]

    def test_pk_huge_int(self):
        """Very large int (Python arbitrary precision) — must work."""
        big = 10 ** 18
        rows = [{"pk": big, "username": "mario"}]
        result = parse_thread_rows(rows, OWN)
        assert result == [(big, "mario")]

    def test_pk_list(self):
        """pk is a list — int(list) raises TypeError → skipped."""
        rows = [{"pk": [1, 2], "username": "mario"}]
        assert parse_thread_rows(rows, OWN) == []

    def test_pk_dict(self):
        """pk is a dict — int(dict) raises TypeError → skipped."""
        rows = [{"pk": {"id": 1}, "username": "mario"}]
        assert parse_thread_rows(rows, OWN) == []

    def test_pk_valid_int(self):
        """Baseline: integer pk works normally."""
        rows = [{"pk": 42, "username": "mario"}]
        assert parse_thread_rows(rows, OWN) == [(42, "mario")]


# ---------------------------------------------------------------------------
# username edge cases
# ---------------------------------------------------------------------------

class TestUsernameEdgeCases:
    def test_username_none(self):
        """username=None is falsy → row skipped."""
        rows = [{"pk": 1, "username": None}]
        assert parse_thread_rows(rows, OWN) == []

    def test_username_empty_string(self):
        """username='' is falsy → row skipped."""
        rows = [{"pk": 2, "username": ""}]
        assert parse_thread_rows(rows, OWN) == []

    def test_username_whitespace_only(self):
        """username='   ' is truthy but blank after strip → row skipped.
        Contract requires non-blank strings only."""
        rows = [{"pk": 3, "username": "   "}]
        result = parse_thread_rows(rows, OWN)
        assert result == []

    def test_username_int_non_string(self):
        """username=123 (int) is not a str → row skipped.
        Downstream code expects str — non-str username must never leak through."""
        rows = [{"pk": 4, "username": 123}]
        result = parse_thread_rows(rows, OWN)
        assert result == []

    def test_username_at_prefixed(self):
        """@mario — valid string, not blank. Accepted."""
        rows = [{"pk": 5, "username": "@mario"}]
        result = parse_thread_rows(rows, OWN)
        assert result == [(5, "@mario")]

    def test_username_normal(self):
        """Baseline: normal username accepted."""
        rows = [{"pk": 6, "username": "mario"}]
        result = parse_thread_rows(rows, OWN)
        assert result == [(6, "mario")]


# ---------------------------------------------------------------------------
# Self-filter (own_pk)
# ---------------------------------------------------------------------------

class TestSelfFilter:
    def test_self_skipped_int_own_pk(self):
        """Row with pk == own_pk is skipped."""
        rows = [{"pk": OWN, "username": "me"}, {"pk": 1, "username": "other"}]
        result = parse_thread_rows(rows, OWN)
        assert result == [(1, "other")]

    def test_self_skipped_str_own_pk(self):
        """own_pk passed as str '999', row pk as int 999.
        int(own_pk) = 999 and int(pk) = 999, so self-filter holds."""
        rows = [{"pk": 999, "username": "me"}, {"pk": 1, "username": "other"}]
        result = parse_thread_rows(rows, "999")
        assert result == [(1, "other")]

    def test_self_skipped_float_own_pk(self):
        """own_pk passed as float 999.0 → int(999.0) = 999.
        Self-filter still holds via int(own_pk) coercion."""
        rows = [{"pk": 999, "username": "me"}, {"pk": 2, "username": "other"}]
        result = parse_thread_rows(rows, 999.0)
        assert result == [(2, "other")]

    def test_self_skipped_str_pk_str_own_pk(self):
        """Both row pk and own_pk are strings that coerce to same int."""
        rows = [{"pk": "999", "username": "me"}, {"pk": "1", "username": "other"}]
        result = parse_thread_rows(rows, "999")
        assert result == [(1, "other")]

    def test_non_self_not_skipped(self):
        """Row with pk != own_pk must NOT be skipped."""
        rows = [{"pk": 1, "username": "mario"}]
        result = parse_thread_rows(rows, OWN)
        assert result == [(1, "mario")]


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------

class TestPerformance:
    def test_large_rows_data(self):
        """10 000 rows must complete quickly (no pathological behavior)."""
        rows = [{"pk": i, "username": f"user{i}"} for i in range(10000)]
        result = parse_thread_rows(rows, OWN)
        # OWN=999 row is skipped; all others present
        assert len(result) == 9999
        assert (OWN, f"user{OWN}") not in result
        assert result[0] == (0, "user0")
        assert result[-1] == (9999, "user9999")


# ---------------------------------------------------------------------------
# Duplicate rows (document contract)
# ---------------------------------------------------------------------------

class TestDuplicateRows:
    def test_duplicate_rows_not_deduped_by_parse(self):
        """parse_thread_rows itself does NOT deduplicate — that is intentional.
        Deduplication is BrowserInboxSource._seen responsibility.
        Assert the actual contract: duplicates pass through."""
        rows = [
            {"pk": 1, "username": "mario"},
            {"pk": 1, "username": "mario"},
        ]
        result = parse_thread_rows(rows, OWN)
        assert result == [(1, "mario"), (1, "mario")]

    def test_duplicate_pk_different_username_not_deduped(self):
        """If DOM somehow gives same pk with different username, both pass through."""
        rows = [
            {"pk": 1, "username": "mario"},
            {"pk": 1, "username": "mario_clone"},
        ]
        result = parse_thread_rows(rows, OWN)
        assert result == [(1, "mario"), (1, "mario_clone")]
