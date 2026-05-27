"""Bot global state — single-row table for kill-switch flag.

When `halted=True`, ALL workers (DM + scraper) refuse to claim new work and
exit gracefully. Only an admin (via API or Telegram /unhalt) can clear it.
"""
from datetime import datetime
from sqlalchemy import String, Text, DateTime, Boolean, Integer
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class BotState(Base):
    __tablename__ = "bot_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    halted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    halted_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    halted_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    halted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    halted_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_resume_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_resume_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
