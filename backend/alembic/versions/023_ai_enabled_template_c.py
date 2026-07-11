"""Template mode: ai_enabled + message_template_c + ai_system_prompt su campaigns.

ai_enabled nasce con server_default TRUE cosi' le campagne esistenti mantengono
il comportamento attuale (AI); subito dopo il default passa a FALSE per le nuove.

Revision ID: 023
Revises: 022
Create Date: 2026-07-11
"""
from alembic import op
import sqlalchemy as sa

revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column("ai_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.alter_column("campaigns", "ai_enabled", server_default=sa.text("false"))
    op.add_column("campaigns", sa.Column("message_template_c", sa.Text(), nullable=True))
    op.add_column("campaigns", sa.Column("ai_system_prompt", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("campaigns", "ai_system_prompt")
    op.drop_column("campaigns", "message_template_c")
    op.drop_column("campaigns", "ai_enabled")
