import uuid
from datetime import datetime
from sqlalchemy import String, Integer, Boolean, Text, DateTime, BigInteger, ForeignKey, UniqueConstraint, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base
import enum


class FollowerStatus(str, enum.Enum):
    pending = "pending"
    bio_scraped = "bio_scraped"
    message_generated = "message_generated"
    # M15 rev: follower blocked pending human review of AI-generated message
    pending_approval = "pending_approval"
    sent = "sent"
    failed = "failed"
    skipped = "skipped"
    replied = "replied"


class Follower(Base):
    __tablename__ = "followers"
    __table_args__ = (
        UniqueConstraint("campaign_id", "ig_user_id", name="uq_campaign_follower"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id: Mapped[str] = mapped_column(String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    ig_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    biography: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_private: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    follower_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    following_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    profile_pic_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Contact info (advanced scraping). Populated from user_info at scrape time.
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    whatsapp: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bio_links: Mapped[str | None] = mapped_column(Text, nullable=True)        # JSON: [{"url","title"}]
    contact_source: Mapped[str | None] = mapped_column(Text, nullable=True)   # JSON: {"phone":"ig_business",...}
    contact_extra: Mapped[str | None] = mapped_column(Text, nullable=True)    # JSON, reserved Fase 2
    status: Mapped[FollowerStatus] = mapped_column(
        SAEnum(FollowerStatus, native_enum=False), nullable=False, default=FollowerStatus.pending
    )
    skip_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Optimistic lock for multi-worker deduplication.
    # Set to account_id when a worker claims this follower; cleared on completion.
    # Stale locks (locked_at older than LOCK_TIMEOUT_MINUTES) are auto-released by cron.
    locked_by_account_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
