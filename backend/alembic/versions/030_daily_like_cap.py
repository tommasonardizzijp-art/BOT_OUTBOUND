"""Tetto giornaliero PERSISTITO ai like ambientali: daily_likes_today +
daily_likes_date su instagram_accounts.

Un like e' un'azione di SCRITTURA (vettore di blocco proprio, peggiore dello
scrape in lettura): oggi browse_feed pesca `max_likes` da
`random.choice([0, 0, 1, 1, 2])` in una variabile LOCALE alla funzione, azzerata
ad ogni chiamata -- cento chiamate = cento budget nuovi, nessun tetto reale.

Stesso schema date-aware di scrape_lookups_today/scrape_lookups_date
(migrazioni 014/018): contatore + giorno UTC "YYYY-MM-DD" come stringa, reset
LAZY in lettura (vedi account_manager.reserve_daily_like) cosi' un riavvio non
azzera il contatore e un cambio di giorno non dipende dal cron daily_reset.

Additiva, nessun ALTER distruttivo.

Revision ID: 030
Revises: 029
Create Date: 2026-08-08
"""
from alembic import op
import sqlalchemy as sa

revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "instagram_accounts",
        sa.Column("daily_likes_today", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "instagram_accounts",
        sa.Column("daily_likes_date", sa.String(length=10), nullable=True),
    )


def downgrade() -> None:
    # batch_alter_table: SQLite non ha DROP COLUMN nativo prima della 3.35 e
    # alembic lo emula ricostruendo la tabella (stesso pattern di 028).
    with op.batch_alter_table("instagram_accounts") as batch:
        batch.drop_column("daily_likes_today")
        batch.drop_column("daily_likes_date")
