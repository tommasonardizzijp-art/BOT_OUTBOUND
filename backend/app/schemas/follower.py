from pydantic import BaseModel
from datetime import datetime
from app.models.follower import FollowerStatus


class FollowerResponse(BaseModel):
    id: str
    campaign_id: str
    ig_user_id: int
    username: str
    full_name: str | None
    biography: str | None
    is_private: bool
    is_verified: bool
    follower_count: int | None
    following_count: int | None
    profile_pic_url: str | None
    external_url: str | None
    status: FollowerStatus
    skip_reason: str | None
    # Pre-generated message text (populated from Message table when available)
    generated_text: str | None = None
    template_variant: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class FollowerListResponse(BaseModel):
    items: list[FollowerResponse]
    total: int
    page: int
    page_size: int
