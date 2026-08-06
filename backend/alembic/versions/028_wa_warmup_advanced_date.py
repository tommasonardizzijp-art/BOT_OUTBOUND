"""warmup_advanced_date su wa_numbers: guardia idempotente per l'avanzamento
automatico giornaliero di warmup_day (M5).

Additiva, nessun ALTER distruttivo. Stesso pattern/tipo di
InstagramAccount.warmup_advanced_date (String(10), nullable, "YYYY-MM-DD"):
se diverso da oggi il numero non e' ancora stato avanzato oggi, sicuro sia
chiamato al boot (main.py lifespan) sia dal cron giornaliero (daily_reset).

Revision ID: 028
Revises: 027
Create Date: 2026-08-06
"""
from alembic import op
import sqlalchemy as sa

revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "wa_numbers",
        sa.Column("warmup_advanced_date", sa.String(length=10), nullable=True),
    )


def downgrade() -> None:
    # batch_alter_table: SQLite non ha DROP COLUMN nativo prima della 3.35 e
    # alembic lo emula ricostruendo la tabella (stesso pattern di 027).
    with op.batch_alter_table("wa_numbers") as batch:
        batch.drop_column("warmup_advanced_date")
