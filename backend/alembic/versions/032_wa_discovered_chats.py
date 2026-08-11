# backend/alembic/versions/032_wa_discovered_chats.py
"""Staging delle chat scoperte dalla Fase A auto-discover WhatsApp.

Tabella nuova, nessun ALTER su tabelle esistenti: additiva e reversibile, il
downgrade la elimina e basta.

Perche' non si riusa wa_contacts (spec 5.4): li' encrypted_phone e phone_hmac
sono NOT NULL, quindi una chat di cui non si legge il numero -- il 100% dei
gruppi e una parte delle 1:1, misurato nel PoC-4 -- non potrebbe esistere. Qui
sono nullable, e la riga resta salvata e marcata.

NUMERO DI REVISIONE: presa la 032 perche' al momento della scrittura (11/08,
verificato su tutti i branch locali e remoti) nessuna 032 esisteva ancora. Il
piano inbox-browser velocita' di un'altra sessione se l'era riservata sulla
carta ma non l'aveva scritta: chi arriva dopo prende la 033. Se questa PR
dovesse restare ferma e l'altra mergiare per prima, rinumerare QUESTO file --
non fidarsi del numero scritto in un piano.

Revision ID: 032
Revises: 031
Create Date: 2026-08-11
"""
from alembic import op
import sqlalchemy as sa

revision = "032"
down_revision = "031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wa_discovered_chats",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id"),
                  nullable=False),
        sa.Column("number_id", sa.String(length=36), sa.ForeignKey("wa_numbers.id"),
                  nullable=False),
        # Nullable per costruzione: quando il titolo E' il numero, il titolo non
        # si salva in chiaro (P12) e questa colonna resta vuota.
        sa.Column("chat_title", sa.String(length=200), nullable=True),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("encrypted_phone", sa.Text(), nullable=True),
        sa.Column("phone_hmac", sa.String(length=64), nullable=True),
        sa.Column("numero_leggibile", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("tipo_chat", sa.String(length=20), nullable=False,
                  server_default="ignoto"),
        sa.Column("source_filtro", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False,
                  server_default="nuovo"),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("number_id", "chat_title",
                            name="uq_wa_discovered_number_title"),
        # In SQL NULL != NULL: la unique sopra non vede due righe con
        # chat_title NULL, ed e' NULL proprio quando il titolo E' il numero
        # (39% delle chat di Primero, P12 impone di non salvarlo in chiaro).
        # Le due unique si coprono a vicenda.
        sa.UniqueConstraint("number_id", "phone_hmac",
                            name="uq_wa_discovered_number_phone"),
    )
    op.create_index("ix_wa_discovered_number_status", "wa_discovered_chats",
                    ["number_id", "status"])
    op.create_index("ix_wa_discovered_phone_hmac", "wa_discovered_chats",
                    ["phone_hmac"])


def downgrade() -> None:
    op.drop_index("ix_wa_discovered_phone_hmac", table_name="wa_discovered_chats")
    op.drop_index("ix_wa_discovered_number_status", table_name="wa_discovered_chats")
    op.drop_table("wa_discovered_chats")
