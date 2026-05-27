import uuid
from datetime import datetime
from sqlalchemy import String, Integer, Text, DateTime, ForeignKey, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base
import enum


class MessageStatus(str, enum.Enum):
    pending = "pending"
    sending = "sending"
    sent = "sent"
    failed = "failed"
    retry = "retry"


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id: Mapped[str] = mapped_column(String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    follower_id: Mapped[str] = mapped_column(String(36), ForeignKey("followers.id", ondelete="CASCADE"), nullable=False)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("instagram_accounts.id", ondelete="SET NULL"), nullable=True)
    generated_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[MessageStatus] = mapped_column(
        SAEnum(MessageStatus, native_enum=False), nullable=False, default=MessageStatus.pending
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # M10: which template variant was used — 'a' (base) or 'b' (A/B test)
    template_variant: Mapped[str | None] = mapped_column(String(1), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    # BUG-NEW-18: track when status last changed (pending→sent/failed/retry)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=True
    )
