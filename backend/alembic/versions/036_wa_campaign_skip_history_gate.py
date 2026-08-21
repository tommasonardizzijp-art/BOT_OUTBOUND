"""wa_campaigns: skip_history_gate, toggle per-campagna per la deroga alla
guardia V2 (contratto §3.1/§3.2)

Sostituisce il CSV in .env (wa_skip_history_gate_campaign_ids, PR #101):
quel meccanismo richiedeva editare un file e riavviare il backend per ogni
campagna nuova -- inutilizzabile quando ogni cliente nuovo arriva con
contatti mai contattati dal bot e OGNI prima campagna e' in questa
situazione. Ora e' un campo impostabile dalla UI alla creazione.

Additiva, nessun ALTER distruttivo.

Revision ID: 036
"""
from alembic import op
import sqlalchemy as sa

revision = "036"
down_revision = "035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default="false": righe gia' esistenti a DB, colonna NOT NULL --
    # senza default il backfill fallisce su Postgres. Stessa convenzione di
    # 027_wa_halted.py.
    op.add_column("wa_campaigns", sa.Column(
        "skip_history_gate", sa.Boolean(), nullable=False, server_default=sa.text("false")))


def downgrade() -> None:
    with op.batch_alter_table("wa_campaigns") as batch:
        batch.drop_column("skip_history_gate")
