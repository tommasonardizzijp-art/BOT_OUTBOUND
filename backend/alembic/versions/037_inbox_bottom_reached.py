"""inbox_bottom_reached: la Fase Lista inbox API sa se ha gia' toccato il fondo

Revision ID: 037
Revises: 036
Create Date: 2026-08-21

NUMERO DI REVISIONE: nata come "036", ma quel numero l'aveva gia' preso un'altra
sessione (036_wa_campaign_skip_history_gate.py) che l'ha anche gia' applicata a
produzione. Due file con lo stesso `revision` sono, per alembic, la STESSA
migration: `upgrade head` da questo branch dichiarava "gia' applicata" e non
eseguiva nessun DDL, in silenzio. Rinumerata la propria, non quella dell'altro —
stessa lezione della 033. Dopo un upgrade su un DB condiviso si verificano le
COLONNE, non `alembic current`.

Serve a distinguere le due modalita' della Fase Lista inbox via API:

- discesa (flag False): si scende verso i thread piu' vecchi partendo da
  `scrape_cursor`. Le pagine di soli contatti gia' noti sono NORMALI e non
  devono fermare la discesa, altrimenti il fondo dell'inbox resta irraggiungibile.
- cima (flag True): il fondo e' gia' stato raggiunto una volta, quindi ogni
  giro riparte dalla cima solo per intercettare i DM nuovi e si ferma appena
  vede `inbox_empty_page_stop` pagine consecutive senza contatti nuovi.

Additiva, NOT NULL con server_default: le campagne esistenti nascono a False
(= non hanno mai toccato il fondo), che e' esattamente il loro stato reale.
"""
import sqlalchemy as sa
from alembic import op

revision = "037"
down_revision = "036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column(
            "inbox_bottom_reached",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Frontiera della discesa, separata da `scrape_cursor` di proposito:
    # scrape_cursor significa "Fase Lista interrotta a meta'" e campaign_control lo
    # legge cosi' per decidere se riprendere la lista o passare alla Fase Bio. La
    # frontiera invece deve sopravvivere alla FINE di un giro, senza per questo
    # tenere la campagna eternamente "in lista".
    op.add_column("campaigns", sa.Column("inbox_deep_cursor", sa.String(255), nullable=True))
    op.add_column(
        "campaigns",
        sa.Column("inbox_deep_pages", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("campaigns", "inbox_deep_pages")
    op.drop_column("campaigns", "inbox_deep_cursor")
    op.drop_column("campaigns", "inbox_bottom_reached")
