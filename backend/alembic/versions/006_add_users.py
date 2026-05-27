"""Add users table for multi-user auth.

Revision ID: 006
Revises: 005
Create Date: 2026-05-08
"""
from alembic import op
import sqlalchemy as sa


revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False, server_default="operator"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("last_login_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
