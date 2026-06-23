"""Inbox: guard 1-account per scrape_mode=dm_threads."""
from app.api.campaigns import inbox_account_count_ok


def test_dm_threads_requires_exactly_one():
    assert inbox_account_count_ok("dm_threads", 1) is True
    assert inbox_account_count_ok("dm_threads", 0) is False
    assert inbox_account_count_ok("dm_threads", 2) is False


def test_other_modes_not_constrained():
    assert inbox_account_count_ok("followers", 0) is True
    assert inbox_account_count_ok("followers", 3) is True
    assert inbox_account_count_ok("following", 2) is True
