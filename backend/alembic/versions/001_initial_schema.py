"""Initial schema — full database setup.

Revision ID: 001
Revises:
Create Date: 2026-04-18
"""
from alembic import op
import sqlalchemy as sa

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "instagram_accounts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("username", sa.String(255), nullable=False, unique=True),
        sa.Column("encrypted_password", sa.Text, nullable=False),
        sa.Column("session_data", sa.Text, nullable=True),
        sa.Column("proxy", sa.String(255), nullable=True),
        sa.Column("status", sa.String(50), nullable=False, server_default="active"),
        sa.Column("daily_message_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("daily_message_limit", sa.Integer, nullable=False, server_default="20"),
        sa.Column("total_messages_sent", sa.Integer, nullable=False, server_default="0"),
        sa.Column("warmup_day", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cooldown_until", sa.DateTime, nullable=True),
        sa.Column("last_activity_at", sa.DateTime, nullable=True),
        sa.Column("last_login_at", sa.DateTime, nullable=True),
        sa.Column("warmup_advanced_date", sa.String(10), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )
    op.create_index("idx_accounts_status", "instagram_accounts", ["status", "cooldown_until"])

    op.create_table(
        "campaigns",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("target_username", sa.String(255), nullable=False),
        sa.Column("target_user_id", sa.BigInteger, nullable=True),
        sa.Column("base_message_template", sa.Text, nullable=False),
        sa.Column("ai_prompt_context", sa.Text, nullable=True),
        sa.Column("message_template_b", sa.Text, nullable=True),
        sa.Column("status", sa.String(50), nullable=False, server_default="draft"),
        sa.Column("total_followers", sa.Integer, nullable=False, server_default="0"),
        sa.Column("messages_sent", sa.Integer, nullable=False, server_default="0"),
        sa.Column("messages_failed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("messages_pending", sa.Integer, nullable=False, server_default="0"),
        sa.Column("daily_limit", sa.Integer, nullable=True),
        sa.Column("require_approval", sa.Integer, nullable=False, server_default="0"),
        sa.Column("approval_sample_size", sa.Integer, nullable=False, server_default="5"),
        sa.Column("scrape_completed_at", sa.DateTime, nullable=True),
        sa.Column("started_at", sa.DateTime, nullable=True),
        sa.Column("completed_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )
    op.create_index("idx_campaigns_status", "campaigns", ["status"])

    op.create_table(
        "campaign_accounts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "campaign_id",
            sa.String(36),
            sa.ForeignKey("campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.String(36),
            sa.ForeignKey("instagram_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("daily_limit_override", sa.Integer, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("campaign_id", "account_id", name="uq_campaign_account"),
    )
    op.create_index("ix_campaign_accounts_campaign_id", "campaign_accounts", ["campaign_id"])

    op.create_table(
        "followers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "campaign_id",
            sa.String(36),
            sa.ForeignKey("campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ig_user_id", sa.BigInteger, nullable=False),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=True),
        sa.Column("biography", sa.Text, nullable=True),
        sa.Column("is_private", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("is_verified", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("follower_count", sa.Integer, nullable=True),
        sa.Column("following_count", sa.Integer, nullable=True),
        sa.Column("profile_pic_url", sa.Text, nullable=True),
        sa.Column("external_url", sa.String(512), nullable=True),
        sa.Column("status", sa.String(50), nullable=False, server_default="pending"),
        sa.Column("skip_reason", sa.String(255), nullable=True),
        sa.Column("locked_by_account_id", sa.String(36), nullable=True),
        sa.Column("locked_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("campaign_id", "ig_user_id", name="uq_campaign_follower"),
    )
    op.create_index("ix_followers_campaign_id", "followers", ["campaign_id"])
    op.create_index("idx_followers_claim", "followers", ["campaign_id", "status", "locked_by_account_id"])
    op.create_index("idx_followers_lock", "followers", ["locked_by_account_id", "locked_at"])
    op.create_index("idx_followers_campaign_updated", "followers", ["campaign_id", "updated_at"])
    op.create_index("idx_followers_status", "followers", ["campaign_id", "status", "updated_at"])

    op.create_table(
        "messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "campaign_id",
            sa.String(36),
            sa.ForeignKey("campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "follower_id",
            sa.String(36),
            sa.ForeignKey("followers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.String(36),
            sa.ForeignKey("instagram_accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("generated_text", sa.Text, nullable=False),
        sa.Column("status", sa.String(50), nullable=False, server_default="pending"),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("template_variant", sa.String(1), nullable=True),
        sa.Column("sent_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_messages_campaign_id", "messages", ["campaign_id"])
    op.create_index("ix_messages_sent_at", "messages", ["sent_at"])
    op.create_index("idx_messages_daily", "messages", ["campaign_id", "status", "sent_at"])

    op.create_table(
        "activity_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "account_id",
            sa.String(36),
            sa.ForeignKey("instagram_accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "campaign_id",
            sa.String(36),
            sa.ForeignKey("campaigns.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("details", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )

    op.create_table(
        "global_contacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("ig_user_id", sa.BigInteger, nullable=False, unique=True),
        sa.Column("username", sa.Text, nullable=True),
        sa.Column("full_name", sa.Text, nullable=True),
        sa.Column("biography", sa.Text, nullable=True),
        sa.Column("last_contacted_at", sa.DateTime, nullable=True),
        sa.Column("contacted_by_campaign_ids", sa.Text, nullable=False, server_default="'[]'"),
        sa.Column("contact_history", sa.Text, nullable=False, server_default="'[]'"),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("global_contacts")
    op.drop_table("activity_logs")
    op.drop_table("messages")
    op.drop_table("followers")
    op.drop_table("campaign_accounts")
    op.drop_table("campaigns")
    op.drop_table("instagram_accounts")
