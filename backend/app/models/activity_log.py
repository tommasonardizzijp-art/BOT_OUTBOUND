import uuid
from datetime import datetime
from sqlalchemy import String, Text, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class ActivityLog(Base):
    __tablename__ = "activity_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("instagram_accounts.id", ondelete="SET NULL"), nullable=True)
    campaign_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON string
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
