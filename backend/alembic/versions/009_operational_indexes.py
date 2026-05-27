"""Operational indexes for hot per-iteration queries.

Revision ID: 009
Revises: 008
Create Date: 2026-05-18
"""
from alembic import op


revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("idx_messages_account_daily", "messages", ["account_id", "status", "sent_at"])
    op.create_index("idx_messages_follower_status", "messages", ["follower_id", "status"])
    op.create_index("idx_messages_status_updated", "messages", ["status", "updated_at"])
    op.create_index("idx_campaign_accounts_account", "campaign_accounts", ["account_id"])
    op.create_index("idx_activity_logs_created", "activity_logs", ["created_at"])


def downgrade() -> None:
    op.drop_index("idx_activity_logs_created", table_name="activity_logs")
    op.drop_index("idx_campaign_accounts_account", table_name="campaign_accounts")
    op.drop_index("idx_messages_status_updated", table_name="messages")
    op.drop_index("idx_messages_follower_status", table_name="messages")
    op.drop_index("idx_messages_account_daily", table_name="messages")
