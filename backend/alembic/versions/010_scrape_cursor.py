"""Add scrape cursor and outcome to campaigns.

Revision ID: 010
Revises: 009
Create Date: 2026-05-18
"""
from alembic import op
import sqlalchemy as sa


revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("campaigns", sa.Column("scrape_cursor", sa.String(255), nullable=True))
    op.add_column("campaigns", sa.Column("scrape_outcome", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("campaigns", "scrape_outcome")
    op.drop_column("campaigns", "scrape_cursor")
