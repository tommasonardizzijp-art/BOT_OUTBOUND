"""current_session_cap su campaigns

Cap random della mini-sessione bio (150-300), persistito perche' il break lungo e'
ancorato con formula deterministica (next_long_break) che deve sopravvivere ai
restart del job (micro-yield). Un cap fisso (250) era una firma da bot.

Revision ID: 021
Revises: 020
Create Date: 2026-07-05
"""
from alembic import op
import sqlalchemy as sa

revision = "021"
down_revision = "020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column("current_session_cap", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("campaigns", "current_session_cap")
