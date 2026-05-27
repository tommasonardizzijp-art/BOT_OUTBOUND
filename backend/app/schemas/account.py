from pydantic import BaseModel, Field
from datetime import datetime
from app.models.account import AccountStatus


class AccountCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=1)
    proxy: str | None = None
    daily_message_limit: int = Field(default=20, ge=1, le=200)
    notes: str | None = None


class AccountUpdate(BaseModel):
    proxy: str | None = None
    daily_message_limit: int | None = Field(default=None, ge=1, le=200)
    notes: str | None = None
    status: AccountStatus | None = None


class AccountResponse(BaseModel):
    id: str
    username: str
    proxy: str | None
    status: AccountStatus
    daily_message_count: int
    daily_message_limit: int
    total_messages_sent: int
    warmup_day: int
    cooldown_until: datetime | None
    last_activity_at: datetime | None
    last_login_at: datetime | None
    notes: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ChallengeVerify(BaseModel):
    code: str = Field(..., min_length=4, max_length=10)
