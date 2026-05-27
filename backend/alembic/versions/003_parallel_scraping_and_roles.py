"""Add parallel scraping + DM: account roles, session break config, auto-generate.

Revision ID: 003
Revises: 002
Create Date: 2026-04-24
"""
from alembic import op
import sqlalchemy as sa

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("campaigns") as batch_op:
        batch_op.add_column(sa.Column("scrape_session_size", sa.Integer(), nullable=False, server_default="250"))
        batch_op.add_column(sa.Column("scrape_break_minutes_min", sa.Integer(), nullable=False, server_default="30"))
        batch_op.add_column(sa.Column("scrape_break_minutes_max", sa.Integer(), nullable=False, server_default="45"))
        batch_op.add_column(sa.Column("bio_fetch_delay_min", sa.Integer(), nullable=False, server_default="5"))
        batch_op.add_column(sa.Column("bio_fetch_delay_max", sa.Integer(), nullable=False, server_default="8"))
        batch_op.add_column(sa.Column("auto_generate", sa.Boolean(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("scrape_break_until", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("scrape_break_prev_status", sa.String(32), nullable=True))

    with op.batch_alter_table("campaign_accounts") as batch_op:
        batch_op.add_column(sa.Column("role", sa.String(16), nullable=False, server_default="both"))


def downgrade() -> None:
    with op.batch_alter_table("campaign_accounts") as batch_op:
        batch_op.drop_column("role")

    with op.batch_alter_table("campaigns") as batch_op:
        batch_op.drop_column("scrape_break_prev_status")
        batch_op.drop_column("scrape_break_until")
        batch_op.drop_column("auto_generate")
        batch_op.drop_column("bio_fetch_delay_max")
        batch_op.drop_column("bio_fetch_delay_min")
        batch_op.drop_column("scrape_break_minutes_max")
        batch_op.drop_column("scrape_break_minutes_min")
        batch_op.drop_column("scrape_session_size")
