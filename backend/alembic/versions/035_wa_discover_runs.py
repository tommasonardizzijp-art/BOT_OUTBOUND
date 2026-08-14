"""wa_discover_runs: storico delle scansioni auto-discover

Revision ID: 035
Revises: 034
Create Date: 2026-08-14

Fino a oggi una scansione non lasciava traccia: l'unico modo di sapere com'era
andata era leggere lo stdout dello script che l'aveva lanciata. Con il lancio
dalla UI (e, dopo, col discover periodico dentro il reply-watcher) quella
traccia diventa l'unico posto dove guardare.

L'indice unico e' PARZIALE su stato='running', non una UniqueConstraint piena
su number_id: le run chiuse devono potersi accumulare (sono lo storico), una
sola per volta puo' essere aperta. Stessa forma della 034 su wa_messages.
"""
import sqlalchemy as sa
from alembic import op

revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None

_INDICE_UNICO = "uq_wa_discover_runs_una_running_per_numero"
_INDICE_STORICO = "ix_wa_discover_runs_number_started"


def upgrade() -> None:
    op.create_table(
        "wa_discover_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("number_id", sa.String(36), sa.ForeignKey("wa_numbers.id"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stato", sa.String(20), nullable=False),
        sa.Column("avviato_da", sa.String(20), nullable=False),
        sa.Column("salvate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("aggiornate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("saltate_gia_note", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("non_verificate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dichiarato", sa.Integer(), nullable=True),
        sa.Column("copertura", sa.Integer(), nullable=True),
        sa.Column("motivo", sa.String(30), nullable=False),
        sa.Column("sync_letta", sa.Integer(), nullable=True),
        sa.Column("sync_stato", sa.String(10), nullable=False),
        sa.Column("errore", sa.Text(), nullable=True),
    )
    op.create_index(_INDICE_STORICO, "wa_discover_runs", ["number_id", "started_at"])
    op.create_index(
        _INDICE_UNICO,
        "wa_discover_runs",
        ["number_id"],
        unique=True,
        postgresql_where=sa.text("stato = 'running'"),
        sqlite_where=sa.text("stato = 'running'"),
    )


def downgrade() -> None:
    op.drop_index(_INDICE_UNICO, table_name="wa_discover_runs")
    op.drop_index(_INDICE_STORICO, table_name="wa_discover_runs")
    op.drop_table("wa_discover_runs")
