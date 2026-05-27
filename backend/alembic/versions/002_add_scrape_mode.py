"""Add scrape_mode to campaigns.

Revision ID: 002
Revises: 001
Create Date: 2026-04-22
"""
from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column("scrape_mode", sa.String(20), nullable=False, server_default="followers"),
    )


def downgrade() -> None:
    op.drop_column("campaigns", "scrape_mode")
