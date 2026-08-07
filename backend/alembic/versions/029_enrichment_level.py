"""enrichment_level su campaigns: livello di arricchimento per campagna.

Additiva, nessun ALTER distruttivo. Backfill che NON cambia il comportamento
di nessuna campagna esistente:
  - ai_enabled = false  -> 'none'      (campagne a template: non serviva l'arricchimento)
  - ai_enabled = true   -> 'contacts'  (personalizzazione AI: continuano come oggi)

Il backfill tocca solo la colonna nuova: nessuno stato di campagna viene
modificato, quindi nessuna campagna ferma viene avviata dalla migrazione.

Revision ID: 029
Revises: 028
Create Date: 2026-08-07
"""
from alembic import op
import sqlalchemy as sa

revision = "029"
down_revision = "028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column("enrichment_level", sa.String(length=10),
                  nullable=False, server_default="none"),
    )
    op.execute(
        "UPDATE campaigns SET enrichment_level = 'contacts' WHERE ai_enabled = true"
    )


def downgrade() -> None:
    # batch_alter_table: SQLite non ha DROP COLUMN nativo prima della 3.35 e
    # alembic lo emula ricostruendo la tabella (stesso pattern di 027/028).
    with op.batch_alter_table("campaigns") as batch:
        batch.drop_column("enrichment_level")
