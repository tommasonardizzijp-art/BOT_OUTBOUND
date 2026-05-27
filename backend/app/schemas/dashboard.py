from pydantic import BaseModel
from datetime import datetime


class DashboardStats(BaseModel):
    total_accounts: int
    active_accounts: int
    accounts_in_cooldown: int
    accounts_banned: int
    total_campaigns: int
    running_campaigns: int
    messages_sent_today: int
    messages_sent_total: int
    messages_failed_total: int
    success_rate: float


class ActivityLogResponse(BaseModel):
    id: str
    account_id: str | None
    campaign_id: str | None
    action: str
    details: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ActivityLogListResponse(BaseModel):
    items: list[ActivityLogResponse]
    total: int


class HourlyPoint(BaseModel):
    hour: str
    count: int


class TimelineResponse(BaseModel):
    data: list[HourlyPoint]
