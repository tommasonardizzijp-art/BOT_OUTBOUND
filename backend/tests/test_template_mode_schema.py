"""Colonne ai_enabled / message_template_c / ai_system_prompt su Campaign + schemi."""
import pytest
from app.models.campaign import Campaign
from app.schemas.campaign import CampaignCreate, CampaignUpdate, CampaignResponse


def test_campaign_model_defaults():
    c = Campaign(name="t")
    # default Python: nuove campagne nascono senza AI
    assert c.ai_enabled is False or c.ai_enabled is None  # None pre-flush, False post-default
    assert c.message_template_c is None
    assert c.ai_system_prompt is None


def test_create_schema_defaults():
    data = CampaignCreate(name="x", target_username="acme",
                          base_message_template="Ciao {nome}, ti scrivo per...")
    assert data.ai_enabled is False
    assert data.message_template_c is None
    assert data.ai_system_prompt is None


def test_update_schema_accepts_new_fields():
    u = CampaignUpdate(ai_enabled=True, message_template_c="Template C abbastanza lungo",
                       ai_system_prompt="Tono formale.")
    assert u.ai_enabled is True
    assert u.message_template_c.startswith("Template C")
    assert u.ai_system_prompt == "Tono formale."


def test_response_schema_has_fields():
    fields = CampaignResponse.model_fields
    assert "ai_enabled" in fields
    assert "message_template_c" in fields
    assert "ai_system_prompt" in fields
