from pydantic import BaseModel, Field, model_validator
from datetime import datetime
from app.models.campaign import CampaignStatus


class CampaignCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    target_username: str | None = Field(default=None, max_length=255)
    source_type: str = Field(default='scrape', pattern='^(scrape|import)$')
    base_message_template: str | None = Field(default=None)
    messaging_enabled: bool = True
    scrape_daily_limit: int | None = Field(default=None, ge=1, le=2000)
    ai_prompt_context: str | None = None
    # M10: optional second template for A/B testing
    message_template_b: str | None = Field(default=None, min_length=10)
    # Max DMs/day across all accounts for this campaign. NULL = unlimited.
    daily_limit: int | None = Field(default=None, ge=1, le=500)
    # M15 rev: approval sampling
    require_approval: bool = False
    approval_sample_size: int = Field(default=5, ge=1, le=50)
    # 'followers' = scrape who follows target; 'following' = scrape who target follows
    scrape_mode: str = Field(default='followers', pattern='^(followers|following|dm_threads)$')
    # Engine estrazione lista per dm_threads (ignorato per followers/following).
    # Default 'api'. 'browser' e' DEPRECATO e no-op: lo scraping via browsing del
    # DOM e' stato rimosso (la lista DM web non espone username/pk) — il backend
    # usa sempre l'API. Il valore resta accettato per retrocompatibilita'.
    inbox_engine: str = Field(default='api', pattern='^(browser|api)$')
    # Motore Fase Bio. 'api' = instagrapi (veloce, consuma cap). 'browser' = Patchright
    # (prudente, no cap API). Vedi migration 022.
    bio_engine: str = Field(default='api', pattern='^(api|browser)$')
    # Session break config
    scrape_session_size: int = Field(default=250, ge=10, le=5000)
    scrape_break_minutes_min: int = Field(default=30, ge=5, le=240)
    scrape_break_minutes_max: int = Field(default=45, ge=5, le=240)
    bio_fetch_delay_min: float = Field(default=5.0, ge=1.0, le=60.0)
    bio_fetch_delay_max: float = Field(default=8.0, ge=1.0, le=120.0)

    @model_validator(mode='after')
    def _check_source(self):
        if self.source_type == 'scrape' and self.scrape_mode != 'dm_threads' \
                and not (self.target_username and self.target_username.strip()):
            raise ValueError("target_username obbligatorio per source_type='scrape'")
        if self.messaging_enabled:
            t = (self.base_message_template or "").strip()
            if len(t) < 10:
                raise ValueError("base_message_template obbligatorio (min 10 caratteri) quando messaging_enabled=True")
        return self


class CampaignUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    base_message_template: str | None = None
    messaging_enabled: bool | None = None
    scrape_daily_limit: int | None = Field(default=None, ge=1, le=2000)
    ai_prompt_context: str | None = None
    # M10: can be set to None to disable A/B testing
    message_template_b: str | None = Field(default=None, min_length=10)
    daily_limit: int | None = Field(default=None, ge=1, le=500)
    # M15 rev: approval sampling
    require_approval: bool | None = None
    approval_sample_size: int | None = Field(default=None, ge=1, le=50)
    scrape_mode: str | None = Field(default=None, pattern='^(followers|following|dm_threads)$')
    inbox_engine: str | None = Field(default=None, pattern='^(browser|api)$')
    bio_engine: str | None = Field(default=None, pattern='^(api|browser)$')
    # Session break config
    scrape_session_size: int | None = Field(default=None, ge=10, le=5000)
    scrape_break_minutes_min: int | None = Field(default=None, ge=5, le=240)
    scrape_break_minutes_max: int | None = Field(default=None, ge=5, le=240)
    bio_fetch_delay_min: float | None = Field(default=None, ge=1.0, le=60.0)
    bio_fetch_delay_max: float | None = Field(default=None, ge=1.0, le=120.0)


class CampaignResponse(BaseModel):
    id: str
    name: str
    target_username: str | None
    source_type: str = 'scrape'
    target_user_id: int | None
    base_message_template: str | None
    ai_prompt_context: str | None
    # M10: A/B testing
    message_template_b: str | None
    status: CampaignStatus
    total_followers: int
    messages_sent: int
    messages_failed: int
    messages_pending: int
    messages_skipped: int = 0
    messages_replied: int = 0
    reply_rate: float = 0.0
    daily_limit: int | None
    messages_sent_today: int = 0  # DMs sent today across all accounts (computed)
    # M15 rev
    require_approval: bool
    approval_sample_size: int
    scrape_mode: str
    inbox_engine: str = 'api'
    bio_engine: str = 'api'
    scrape_completed_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    # Session break config
    scrape_session_size: int = 250
    scrape_break_minutes_min: int = 30
    scrape_break_minutes_max: int = 45
    bio_fetch_delay_min: float = 5.0
    bio_fetch_delay_max: float = 8.0
    auto_generate: bool = False
    messaging_enabled: bool = True
    scrape_daily_limit: int | None = None
    scrape_break_until: datetime | None = None
    scrape_cursor: str | None = None
    scrape_outcome: str | None = None
    list_target: int | None = None
    bio_target: int | None = None
    list_progress: dict | None = None
    bio_progress: dict | None = None

    model_config = {"from_attributes": True}
