"""segnalibro della Fase Lista inbox via browser

Revision ID: 033
Revises: 032
Create Date: 2026-08-11

Additiva e nullable: su main nessun codice legge ancora queste colonne, quindi
si puo' applicare al DB condiviso senza cambiare comportamento.

NUMERO DI REVISIONE: il piano da cui parte questa migration l'aveva prenotata
come "032", ma quella e' stata presa nel frattempo da un'altra sessione
(032_wa_discovered_chats.py, staging autodiscover WhatsApp) — lo dice esplicita
la sua stessa docstring: "chi arriva dopo prende la 033". Non rinumerare quella,
rinumerare la propria: e' la lezione delle migration 027 e 029 applicata al
caso in cui non e' la prima ad arrivare.
"""
import sqlalchemy as sa
from alembic import op

revision = "033"
down_revision = "032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("campaigns", sa.Column("inbox_cursor_at", sa.DateTime(), nullable=True))
    op.add_column("campaigns", sa.Column("inbox_cursor_updated_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("campaigns", "inbox_cursor_updated_at")
    op.drop_column("campaigns", "inbox_cursor_at")
