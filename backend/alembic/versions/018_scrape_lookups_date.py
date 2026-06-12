"""Lazy daily reset for scrape cap: scrape_lookups_date on instagram_accounts.

Il contatore `scrape_lookups_today` veniva azzerato solo dal cron `daily_reset`
(worker separato, mezzanotte UTC). Se il cron non gira / il bot e' spento a
quell'ora, il contatore resta al valore del giorno prima e la Fase Bio parte
gia' a cap. Questa colonna registra il giorno (UTC, "YYYY-MM-DD") a cui il
contatore si riferisce: se diverso da oggi il conteggio vale 0 (reset lazy al
primo bump), indipendentemente dal cron.

Revision ID: 018
Revises: 017
Create Date: 2026-06-11
"""
from alembic import op
import sqlalchemy as sa

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "instagram_accounts",
        sa.Column("scrape_lookups_date", sa.String(length=10), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("instagram_accounts", "scrape_lookups_date")
