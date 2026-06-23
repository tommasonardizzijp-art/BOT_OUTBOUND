"""Inbox DM scraping: colonna inbox_engine su campaigns.

Revision ID: 019
Revises: 018
Create Date: 2026-06-23
"""
from alembic import op
import sqlalchemy as sa

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column("inbox_engine", sa.String(length=10), nullable=False, server_default="browser"),
    )


def downgrade() -> None:
    op.drop_column("campaigns", "inbox_engine")
