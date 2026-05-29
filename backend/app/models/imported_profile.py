import uuid
from datetime import datetime
from sqlalchemy import String, DateTime, BigInteger, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class ImportedProfile(Base):
    """Staging row for an imported Instagram profile awaiting resolution into a Follower."""
    __tablename__ = "imported_profiles"
    __table_args__ = (
        UniqueConstraint("campaign_id", "username", name="uq_import_campaign_username"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    raw_input: Mapped[str] = mapped_column(String(512), nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    # pending | resolved | not_found | private | error
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    ig_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
