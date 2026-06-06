import pytest
from pydantic import ValidationError
from app.schemas.campaign import CampaignCreate


def test_messaging_enabled_requires_template():
    with pytest.raises(ValidationError):
        CampaignCreate(name="x", target_username="shop", messaging_enabled=True,
                       base_message_template=None)


def test_messaging_disabled_allows_empty_template():
    c = CampaignCreate(name="x", target_username="shop", messaging_enabled=False)
    assert c.messaging_enabled is False
    assert c.base_message_template in (None, "")


def test_messaging_enabled_with_template_ok():
    c = CampaignCreate(name="x", target_username="shop", messaging_enabled=True,
                       base_message_template="Ciao {username}, ti scrivo per...")
    assert c.messaging_enabled is True


def test_default_messaging_enabled_true_requires_template():
    # Backward compat: omitting messaging_enabled defaults to True → template required.
    with pytest.raises(ValidationError):
        CampaignCreate(name="x", target_username="shop")
