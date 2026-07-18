"""Quarto template opzionale (variante 'd') per il rendering locale A/B/C/D.

Simmetrico a message_template_b/c: colonna Text nullable, nessun backfill.

Revision ID: 024
Revises: 023
Create Date: 2026-07-18
"""
from alembic import op
import sqlalchemy as sa

revision = "024"
down_revision = "023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("campaigns", sa.Column("message_template_d", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("campaigns", "message_template_d")
