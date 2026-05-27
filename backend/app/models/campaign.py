import uuid
from datetime import datetime
from sqlalchemy import String, Integer, Float, Boolean, Text, DateTime, BigInteger, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base
import enum


class CampaignStatus(str, enum.Enum):
    draft = "draft"
    scraping = "scraping"
    scraping_break = "scraping_break"
    scraping_and_running = "scraping_and_running"
    ready = "ready"
    running = "running"
    paused = "paused"
    completed = "completed"
    error = "error"


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    target_username: Mapped[str] = mapped_column(String(255), nullable=False)
    target_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    base_message_template: Mapped[str] = mapped_column(Text, nullable=False)
    ai_prompt_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    # M10: optional second template for A/B testing (50/50 random split)
    message_template_b: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[CampaignStatus] = mapped_column(
        SAEnum(CampaignStatus, native_enum=False), nullable=False, default=CampaignStatus.draft
    )
    total_followers: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    messages_sent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    messages_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    messages_pending: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Maximum DMs to send per day across ALL accounts for this campaign. NULL = unlimited.
    daily_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # M15 rev: if True, a sample of AI-generated messages must be approved before sending
    require_approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # How many messages to put in approval queue per pre-gen run (default 5)
    approval_sample_size: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    # 'followers' = scrape who follows target; 'following' = scrape who target follows
    scrape_mode: Mapped[str] = mapped_column(String(20), nullable=False, default='followers')
    # Parallel scraping + DM config (per-campaign)
    scrape_session_size: Mapped[int] = mapped_column(Integer, default=250, nullable=False)
    scrape_break_minutes_min: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    scrape_break_minutes_max: Mapped[int] = mapped_column(Integer, default=45, nullable=False)
    bio_fetch_delay_min: Mapped[float] = mapped_column(Float, default=5.0, nullable=False)
    bio_fetch_delay_max: Mapped[float] = mapped_column(Float, default=8.0, nullable=False)
    auto_generate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    scrape_break_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scrape_break_prev_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scrape_cursor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 'completed' | 'partial' | 'rate_limited' — esito ultimo scraping
    scrape_outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)
    scrape_completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
