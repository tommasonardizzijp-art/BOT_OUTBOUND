from app.services.import_resolver import classify_resolution
from instagrapi.exceptions import UserNotFound


class _FakeUser:
    def __init__(self, is_private):
        self.is_private = is_private


def test_classify_success_public():
    status, create = classify_resolution(_FakeUser(is_private=False), None)
    assert status == "resolved" and create is True

def test_classify_private_still_creates():
    status, create = classify_resolution(_FakeUser(is_private=True), None)
    assert status == "private" and create is True

def test_classify_not_found():
    status, create = classify_resolution(None, UserNotFound("nope"))
    assert status == "not_found" and create is False

def test_classify_generic_error():
    status, create = classify_resolution(None, ValueError("boom"))
    assert status == "error" and create is False
