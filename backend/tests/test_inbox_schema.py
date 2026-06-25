"""Inbox: validazione schema CampaignCreate per dm_threads."""
import pytest
from pydantic import ValidationError
from app.schemas.campaign import CampaignCreate


def test_dm_threads_mode_accepted_without_target():
    c = CampaignCreate(name="x", scrape_mode="dm_threads", messaging_enabled=False)
    assert c.scrape_mode == "dm_threads"
    assert c.inbox_engine == "api"  # default (browser deprecato/no-op)
    assert c.target_username is None


def test_inbox_engine_api_accepted():
    c = CampaignCreate(name="x", scrape_mode="dm_threads", inbox_engine="api", messaging_enabled=False)
    assert c.inbox_engine == "api"


def test_inbox_engine_invalid_rejected():
    with pytest.raises(ValidationError):
        CampaignCreate(name="x", scrape_mode="dm_threads", inbox_engine="selenium", messaging_enabled=False)


def test_scrape_mode_still_requires_target_for_followers():
    with pytest.raises(ValidationError):
        CampaignCreate(name="x", scrape_mode="followers", messaging_enabled=False)
