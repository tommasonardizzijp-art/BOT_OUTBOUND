from pydantic import BaseModel
from datetime import datetime
from app.models.message import MessageStatus


class MessageResponse(BaseModel):
    id: str
    campaign_id: str
    campaign_name: str | None = None
    follower_id: str
    follower_username: str | None = None
    follower_full_name: str | None = None
    account_id: str | None
    account_username: str | None = None
    generated_text: str
    status: MessageStatus
    has_reply: bool = False
    error_message: str | None
    retry_count: int
    # M10: A/B testing variant ('a' or 'b', None for messages created before M10)
    template_variant: str | None
    sent_at: datetime | None
    created_at: datetime
    updated_at: datetime | None

    model_config = {"from_attributes": True}


class MessageListResponse(BaseModel):
    items: list[MessageResponse]
    total: int
    page: int
    page_size: int


class MessageStats(BaseModel):
    total_sent: int
    total_failed: int
    total_replied: int
    success_rate: float   # sent / (sent + failed) * 100
    reply_rate: float     # replied / sent * 100
