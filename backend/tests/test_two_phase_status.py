"""Two-phase: nuovi stati campagna e colonne target."""
from app.models.campaign import Campaign, CampaignStatus


def test_new_statuses_exist():
    assert CampaignStatus.listing.value == "listing"
    assert CampaignStatus.listing_break.value == "listing_break"


def test_target_columns_present():
    cols = Campaign.__table__.columns.keys()
    assert "list_target" in cols
    assert "bio_target" in cols
