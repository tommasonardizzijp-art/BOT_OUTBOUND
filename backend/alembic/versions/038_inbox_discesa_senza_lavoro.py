"""Contatore PERSISTITO delle pagine di discesa che non producono nulla.

Perche' persistito e non una variabile locale: `run_inbox_list` esce ogni
`inbox_session_pages` (15) per la pausa di sessione, quindi un contatore locale
non supera mai 15 e una soglia piu' alta non scatta MAI. E' lo stesso difetto che
il vecchio tetto a pagine denunciava — "il budget di sessione chiude un giro e ne
fa ripartire un altro: da solo non garantisce che la discesa finisca" — e la
prima versione di questa rete ci e' ricascata dentro.

A cosa serve: e' l'unica garanzia che la discesa termini. Esiste uno scenario che
nessuna delle altre guardie vede — pagine piene, cursori sempre nuovi, utenti veri
ma tutti gia' in lista — in cui il giro scenderebbe per sessioni all'infinito
bruciando chiamate API a vuoto. Instagram puo' tenere `has_older` vero per sempre
(comportamento gia' documentato nel modulo), quindi il fondo da solo non basta.

Revision ID: 038
Revises: 037
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = "038"
down_revision = "037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column(
            "inbox_deep_senza_lavoro",
            sa.Integer(),
            nullable=False,
            server_default=text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("campaigns", "inbox_deep_senza_lavoro")
