"""Two-phase scraping: list_target / bio_target on campaigns.

Revision ID: 016
Revises: 015
Create Date: 2026-06-09
"""
from alembic import op
import sqlalchemy as sa

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("campaigns", sa.Column("list_target", sa.Integer(), nullable=True))
    op.add_column("campaigns", sa.Column("bio_target", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("campaigns", "bio_target")
    op.drop_column("campaigns", "list_target")
