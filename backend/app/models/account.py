import uuid
from datetime import datetime
from sqlalchemy import String, Integer, Boolean, DateTime, Text, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base
import enum


class AccountStatus(str, enum.Enum):
    active = "active"
    warming_up = "warming_up"
    cooldown = "cooldown"
    banned = "banned"
    challenge_required = "challenge_required"
    disabled = "disabled"


class InstagramAccount(Base):
    __tablename__ = "instagram_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    encrypted_password: Mapped[str] = mapped_column(Text, nullable=False)
    session_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    proxy: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[AccountStatus] = mapped_column(
        SAEnum(AccountStatus, native_enum=False), nullable=False, default=AccountStatus.active
    )
    daily_message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    scrape_lookups_today: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    daily_message_limit: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    total_messages_sent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    warmup_day: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    warmup_advanced_date: Mapped[str | None] = mapped_column(String(10), nullable=True)  # ISO date "YYYY-MM-DD"
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
