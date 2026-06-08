from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


LeadRunStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
LeadQualificationStatusValue = Literal["match", "no_match", "ambiguous", "error"]


class LeadThresholdsMixin(BaseModel):
    pass_threshold: int = Field(default=80, ge=0, le=100)
    reject_threshold: int = Field(default=25, ge=0, le=100)
    ai_review_min_score: int = Field(default=26, ge=0, le=100)
    ai_review_max_score: int = Field(default=79, ge=0, le=100)
    max_run_size: int = Field(default=5000, ge=1, le=5000)

    @model_validator(mode="after")
    def _check_thresholds(self):
        if self.reject_threshold >= self.pass_threshold:
            raise ValueError("reject_threshold deve essere minore di pass_threshold")
        if self.ai_review_min_score > self.ai_review_max_score:
            raise ValueError("ai_review_min_score deve essere <= ai_review_max_score")
        return self


class CompileProfileRequest(BaseModel):
    description: str = Field(..., min_length=20)


class CompileProfileResponse(LeadThresholdsMixin):
    name_suggestion: str
    compiled_rules: dict[str, Any]


class LeadTargetProfileCreate(LeadThresholdsMixin):
    name: str = Field(..., min_length=1, max_length=255)
    description: str = Field(..., min_length=20)
    compiled_rules: dict[str, Any]


class LeadTargetProfileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, min_length=20)
    compiled_rules: dict[str, Any] | None = None
    pass_threshold: int | None = Field(default=None, ge=0, le=100)
    reject_threshold: int | None = Field(default=None, ge=0, le=100)
    ai_review_min_score: int | None = Field(default=None, ge=0, le=100)
    ai_review_max_score: int | None = Field(default=None, ge=0, le=100)
    max_run_size: int | None = Field(default=None, ge=1, le=5000)


class LeadTargetProfileResponse(LeadThresholdsMixin):
    id: str
    name: str
    description: str
    compiled_rules: dict[str, Any]
    rules_hash: str
    created_at: datetime
    updated_at: datetime


class LeadQualificationFilters(BaseModel):
    date_from: str | None = None
    date_to: str | None = None
    campaign_ids: list[str] = Field(default_factory=list)
    scraping_account_ids: list[str] = Field(default_factory=list)
    has_phone: bool = False
    has_email: bool = False
    min_followers: int | None = Field(default=None, ge=0)
    max_leads: int = Field(default=5000, ge=1, le=5000)
    skip_existing_same_rules: bool = True


class LeadQualificationEstimateRequest(BaseModel):
    target_profile_id: str
    filters: LeadQualificationFilters


class LeadQualificationEstimateResponse(BaseModel):
    candidate_count: int
    already_qualified_same_rules: int
    will_process: int
    over_limit: bool
    max_run_size: int


class LeadQualificationRunCreate(BaseModel):
    target_profile_id: str
    filters: LeadQualificationFilters


class LeadQualificationRunResponse(BaseModel):
    id: str
    target_profile_id: str
    target_profile_name: str | None = None
    target_description: str | None = None
    filters: dict[str, Any]
    rules_hash: str
    status: LeadRunStatus
    total_candidates: int
    skipped_existing: int
    processed_count: int
    matched_count: int
    no_match_count: int
    ambiguous_count: int
    ai_reviewed_count: int
    error_count: int
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class LeadQualificationResultResponse(BaseModel):
    id: str
    target_profile_id: str
    target_profile_name: str
    run_id: str
    ig_user_id: int
    username: str | None
    full_name: str | None
    biography: str | None
    phone: str | None
    email: str | None
    whatsapp: str | None
    external_url: str | None
    bio_links: list[Any]
    status: LeadQualificationStatusValue
    confidence_score: int
    deterministic_score: int
    ai_score: int | None
    ai_used: bool
    matched_signals: list[Any]
    negative_signals: list[Any]
    reason: str | None
    first_seen_at: datetime | None
    created_at: datetime


class LeadQualificationResultListResponse(BaseModel):
    items: list[LeadQualificationResultResponse]
    total: int
    page: int
    page_size: int
