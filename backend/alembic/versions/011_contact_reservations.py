"""Temporary contact reservations.

Revision ID: 011
Revises: 010
Create Date: 2026-05-18
"""
from alembic import op
import sqlalchemy as sa


revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "contact_reservations",
        sa.Column("ig_user_id", sa.BigInteger(), primary_key=True),
        sa.Column("owner_job", sa.String(128), nullable=False),
        sa.Column("campaign_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_index("idx_reservations_expiry", "contact_reservations", ["expires_at"])


def downgrade() -> None:
    op.drop_index("idx_reservations_expiry", table_name="contact_reservations")
    op.drop_table("contact_reservations")
