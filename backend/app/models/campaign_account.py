import uuid
from datetime import datetime
from sqlalchemy import String, Integer, Boolean, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class CampaignAccount(Base):
    """
    Junction table: assigns one or more Instagram accounts to a campaign.

    Each row means "account X is authorized to send DMs for campaign Y".
    `daily_limit_override` overrides the account's global daily_message_limit
    for this specific campaign. NULL = use the account's global limit.
    """
    __tablename__ = "campaign_accounts"
    __table_args__ = (
        UniqueConstraint("campaign_id", "account_id", name="uq_campaign_account"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("instagram_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Per-account, per-campaign daily DM cap. NULL = fall back to account's global limit.
    daily_limit_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Role / capabilities. Base: 'scraping' (bio only), 'dm' (send only),
    # 'both' (scrape+dm). Inbox-capable variants (DM-thread listing, max 1 per
    # campaign): 'inbox', 'inbox_scraping', 'inbox_dm', 'inbox_both'.
    # Single source of truth: app.utils.roles. Longest value 'inbox_scraping' = 14 chars.
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="both")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
