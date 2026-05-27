import uuid
from datetime import datetime
from sqlalchemy import String, BigInteger, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class GlobalContact(Base):
    __tablename__ = "global_contacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    ig_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(Text, nullable=True)
    full_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    biography: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_contacted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    contacted_by_campaign_ids: Mapped[str] = mapped_column(Text, default="[]", nullable=False)  # JSON array (legacy)
    # Richer contact history: [{campaign_id, campaign_name, account_id, account_username, contacted_at}]
    contact_history: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
