"""Fase Bio: colonna bio_engine su campaigns.

Revision ID: 022
Revises: 021
Create Date: 2026-07-06
"""
from alembic import op
import sqlalchemy as sa

revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column("bio_engine", sa.String(length=10), nullable=False, server_default="api"),
    )


def downgrade() -> None:
    op.drop_column("campaigns", "bio_engine")
