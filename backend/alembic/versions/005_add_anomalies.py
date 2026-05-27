"""Add anomalies table for auto-stop + Telegram alert system.

Revision ID: 005
Revises: 004
Create Date: 2026-05-08
"""
from alembic import op
import sqlalchemy as sa


revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "anomalies",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "campaign_id",
            sa.String(length=36),
            sa.ForeignKey("campaigns.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "account_id",
            sa.String(length=36),
            sa.ForeignKey("instagram_accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False, server_default="info"),
        sa.Column("details", sa.Text, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("acknowledged_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_anomalies_kind_created", "anomalies", ["kind", "created_at"])
    op.create_index("ix_anomalies_campaign_created", "anomalies", ["campaign_id", "created_at"])
    op.create_index("ix_anomalies_account_created", "anomalies", ["account_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_anomalies_account_created", table_name="anomalies")
    op.drop_index("ix_anomalies_campaign_created", table_name="anomalies")
    op.drop_index("ix_anomalies_kind_created", table_name="anomalies")
    op.drop_table("anomalies")
