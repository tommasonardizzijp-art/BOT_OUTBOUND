from datetime import datetime
from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=255)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds
    user: "UserResponse"


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=255)
    role: str = Field(default="operator", pattern="^(admin|operator)$")


class UserUpdate(BaseModel):
    role: str | None = Field(default=None, pattern="^(admin|operator)$")
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=255)


class UserResponse(BaseModel):
    id: str
    email: str
    role: str
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None

    model_config = {"from_attributes": True}


# Resolve forward ref
TokenResponse.model_rebuild()
