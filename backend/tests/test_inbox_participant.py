"""Inbox: estrazione del partecipante 1-a-1 da un thread."""
from types import SimpleNamespace as NS
from app.services.inbox_source import extract_thread_participant

OWN = 999


def _u(pk, username):
    return NS(pk=pk, username=username)


def test_one_to_one_outbound():
    users = [_u(123, "mario")]  # noi non siamo in thread.users di instagrapi
    assert extract_thread_participant(users, OWN) == (123, "mario")


def test_one_to_one_with_self_present():
    users = [_u(OWN, "me"), _u(123, "mario")]
    assert extract_thread_participant(users, OWN) == (123, "mario")


def test_group_skipped():
    users = [_u(123, "mario"), _u(456, "lucia")]
    assert extract_thread_participant(users, OWN) is None


def test_empty_or_self_only_skipped():
    assert extract_thread_participant([], OWN) is None
    assert extract_thread_participant([_u(OWN, "me")], OWN) is None


def test_missing_username_skipped():
    assert extract_thread_participant([_u(123, None)], OWN) is None


def test_str_pk_coerced():
    assert extract_thread_participant([_u("123", "mario")], OWN) == (123, "mario")
