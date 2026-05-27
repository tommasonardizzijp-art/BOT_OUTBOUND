"""Add bot_state table for global kill-switch.

Revision ID: 007
Revises: 006
Create Date: 2026-05-09
"""
from alembic import op
import sqlalchemy as sa


revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bot_state",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("halted", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("halted_reason", sa.Text, nullable=True),
        sa.Column("halted_kind", sa.String(length=64), nullable=True),
        sa.Column("halted_at", sa.DateTime, nullable=True),
        sa.Column("halted_by", sa.String(length=255), nullable=True),
        sa.Column("last_resume_at", sa.DateTime, nullable=True),
        sa.Column("last_resume_by", sa.String(length=255), nullable=True),
    )
    # Seed singleton row. Use 'false' literal (portable SQLite + Postgres).
    op.execute("INSERT INTO bot_state (id, halted) VALUES (1, false)")


def downgrade() -> None:
    op.drop_table("bot_state")
