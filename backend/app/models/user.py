import uuid
from datetime import datetime
from sqlalchemy import String, DateTime, Boolean, Index
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class User(Base):
    """Auth user. Two roles: 'admin' (full control) and 'operator' (read + run)."""
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="operator")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_users_email", "email", unique=True),
    )
