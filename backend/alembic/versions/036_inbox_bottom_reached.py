"""inbox_bottom_reached: la Fase Lista inbox API sa se ha gia' toccato il fondo

Revision ID: 036
Revises: 035
Create Date: 2026-08-21

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

revision = "036"
down_revision = "035"
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


def downgrade() -> None:
    op.drop_column("campaigns", "inbox_bottom_reached")
