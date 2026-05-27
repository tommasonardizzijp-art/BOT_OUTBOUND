from datetime import datetime
from pydantic import BaseModel


class BotStateResponse(BaseModel):
    halted: bool
    halted_reason: str | None = None
    halted_kind: str | None = None
    halted_at: datetime | None = None
    halted_by: str | None = None
    last_resume_at: datetime | None = None
    last_resume_by: str | None = None

    model_config = {"from_attributes": True}


class BotHaltRequest(BaseModel):
    reason: str
    kind: str | None = None

