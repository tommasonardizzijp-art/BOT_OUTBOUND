from pydantic import BaseModel, Field
from datetime import datetime
from typing import Literal


class CampaignAccountAssign(BaseModel):
    account_id: str
    # NULL = use account's global daily_message_limit (possibly warmup-adjusted)
    daily_limit_override: int | None = Field(default=None, ge=1, le=200)
    role: Literal["scraping", "dm", "both"] = "both"


class CampaignAccountUpdate(BaseModel):
    # Set to None to clear override (revert to account global limit)
    daily_limit_override: int | None = Field(default=None, ge=1, le=200)
    is_active: bool | None = None
    role: Literal["scraping", "dm", "both"] | None = None


class CampaignAccountResponse(BaseModel):
    id: str
    campaign_id: str
    account_id: str
    account_username: str  # joined from instagram_accounts
    daily_limit_override: int | None
    is_active: bool
    role: str = "both"
    created_at: datetime

    model_config = {"from_attributes": True}
