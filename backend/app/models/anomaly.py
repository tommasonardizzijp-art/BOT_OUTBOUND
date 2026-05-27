import uuid
from datetime import datetime
from sqlalchemy import String, Text, DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class Anomaly(Base):
    """Anomalous events detected by the bot — used by the auto-stop logic and admin UI."""
    __tablename__ = "anomalies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True
    )
    account_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("instagram_accounts.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    details: Mapped[str] = mapped_column(Text, nullable=False, default="{}")  # JSON string
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_anomalies_kind_created", "kind", "created_at"),
        Index("ix_anomalies_campaign_created", "campaign_id", "created_at"),
        Index("ix_anomalies_account_created", "account_id", "created_at"),
    )
