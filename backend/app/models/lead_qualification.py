import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Enum as SAEnum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LeadQualificationRunStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class LeadQualificationStatus(str, enum.Enum):
    match = "match"
    no_match = "no_match"
    ambiguous = "ambiguous"
    error = "error"


class LeadTargetProfile(Base):
    __tablename__ = "lead_target_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    compiled_rules: Mapped[str] = mapped_column(Text, nullable=False)
    rules_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    pass_threshold: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    reject_threshold: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ai_review_min_score: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    ai_review_max_score: Mapped[int] = mapped_column(Integer, default=9, nullable=False)
    max_run_size: Mapped[int] = mapped_column(Integer, default=5000, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class LeadQualificationRun(Base):
    __tablename__ = "lead_qualification_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    target_profile_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("lead_target_profiles.id", ondelete="CASCADE"), nullable=False
    )
    target_name: Mapped[str] = mapped_column(String(255), nullable=False)
    target_description: Mapped[str] = mapped_column(Text, nullable=False)
    compiled_rules: Mapped[str] = mapped_column(Text, nullable=False)
    filters: Mapped[str] = mapped_column(Text, nullable=False)
    rules_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    pass_threshold: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    reject_threshold: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ai_review_min_score: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    ai_review_max_score: Mapped[int] = mapped_column(Integer, default=9, nullable=False)
    status: Mapped[LeadQualificationRunStatus] = mapped_column(
        SAEnum(LeadQualificationRunStatus, native_enum=False),
        default=LeadQualificationRunStatus.queued,
        nullable=False,
    )
    total_candidates: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_existing: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    matched_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    no_match_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ambiguous_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ai_reviewed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class LeadQualification(Base):
    __tablename__ = "lead_qualifications"
    __table_args__ = (
        UniqueConstraint("run_id", "global_contact_id", name="uq_lq_run_contact"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    global_contact_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("global_contacts.id", ondelete="CASCADE"), nullable=False
    )
    ig_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_profile_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("lead_target_profiles.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("lead_qualification_runs.id", ondelete="CASCADE"), nullable=False
    )
    rules_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    deterministic_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ai_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[LeadQualificationStatus] = mapped_column(
        SAEnum(LeadQualificationStatus, native_enum=False), nullable=False
    )
    matched_signals: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    negative_signals: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    ai_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ai_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_used: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
