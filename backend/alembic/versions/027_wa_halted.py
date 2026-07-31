"""kill-switch per-canale: wa_halted su bot_state

Additiva, nessun ALTER distruttivo. down_revision = "025": la 026 e' il
numero riservato a M2 (contratto §6.1) e potrebbe non esistere mai. Se al
rebase su main la 026 c'e', questo valore diventa "026" e si rifa' il ciclo
su-giu'-su (contratto §6.1).

Revision ID: 027
"""
from alembic import op
import sqlalchemy as sa

revision = "027"
down_revision = "025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default="0": la riga singleton esiste gia' in produzione e la
    # colonna e' NOT NULL -- senza default il backfill fallisce su Postgres.
    op.add_column("bot_state", sa.Column("wa_halted", sa.Boolean(), nullable=False,
                                          server_default=sa.text("0")))
    op.add_column("bot_state", sa.Column("wa_halted_reason", sa.Text(), nullable=True))
    op.add_column("bot_state", sa.Column("wa_halted_at", sa.DateTime(), nullable=True))
    op.add_column("bot_state", sa.Column("wa_halted_by", sa.String(255), nullable=True))


def downgrade() -> None:
    # batch_alter_table: SQLite non ha DROP COLUMN nativo prima della 3.35 e
    # alembic lo emula ricostruendo la tabella. Su Postgres e' un DROP COLUMN
    # normale.
    with op.batch_alter_table("bot_state") as batch:
        batch.drop_column("wa_halted_by")
        batch.drop_column("wa_halted_at")
        batch.drop_column("wa_halted_reason")
        batch.drop_column("wa_halted")
