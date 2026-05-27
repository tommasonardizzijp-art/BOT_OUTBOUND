"""Account lease columns.

Revision ID: 012
Revises: 011
Create Date: 2026-05-18
"""
from alembic import op
import sqlalchemy as sa


revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("instagram_accounts", sa.Column("lease_owner", sa.String(128), nullable=True))
    op.add_column("instagram_accounts", sa.Column("lease_expires_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("instagram_accounts", "lease_expires_at")
    op.drop_column("instagram_accounts", "lease_owner")
