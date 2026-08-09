# backend/alembic/versions/031_inbox_browser_fields.py
"""Campi raccolti dal motore inbox browser: last_message_at/from/text + source_channel.

Il motore browser apre le chat e legge dati che l'API non espone (testo integrale
dell'ultimo messaggio, chi l'ha scritto, data assoluta). source_channel serve a
sapere da dove arriva un dato: le schede raccolte via API hanno full_name NULL e
non sono riconoscibili dal nome visualizzato.

Tutte nullable: additiva, nessun ALTER distruttivo, le schede esistenti restano
valide con i campi vuoti.

Revision ID: 031
Revises: 030
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa

revision = "031"
down_revision = "030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("followers", sa.Column("last_message_at", sa.DateTime(), nullable=True))
    op.add_column("followers", sa.Column("last_message_from", sa.String(length=10), nullable=True))
    op.add_column("followers", sa.Column("last_message_text", sa.Text(), nullable=True))
    op.add_column("followers", sa.Column("source_channel", sa.String(length=10), nullable=True))


def downgrade() -> None:
    # batch_alter_table: SQLite non ha DROP COLUMN nativo prima della 3.35
    # (stesso pattern di 028 e 030).
    with op.batch_alter_table("followers") as batch:
        batch.drop_column("last_message_at")
        batch.drop_column("last_message_from")
        batch.drop_column("last_message_text")
        batch.drop_column("source_channel")
