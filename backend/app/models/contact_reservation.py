from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ContactReservation(Base):
    __tablename__ = "contact_reservations"

    ig_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_job: Mapped[str] = mapped_column(String(128), nullable=False)
    campaign_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
