"""Advanced scraping: contact columns + messaging toggle + scrape cap.

Revision ID: 014
Revises: 013
Create Date: 2026-06-06
"""
from alembic import op
import sqlalchemy as sa

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # followers: contact columns
    op.add_column("followers", sa.Column("phone", sa.String(64), nullable=True))
    op.add_column("followers", sa.Column("email", sa.String(255), nullable=True))
    op.add_column("followers", sa.Column("whatsapp", sa.String(255), nullable=True))
    op.add_column("followers", sa.Column("bio_links", sa.Text(), nullable=True))
    op.add_column("followers", sa.Column("contact_source", sa.Text(), nullable=True))
    op.add_column("followers", sa.Column("contact_extra", sa.Text(), nullable=True))

    # global_contacts: contact columns + provenance
    op.add_column("global_contacts", sa.Column("phone", sa.String(64), nullable=True))
    op.add_column("global_contacts", sa.Column("email", sa.String(255), nullable=True))
    op.add_column("global_contacts", sa.Column("whatsapp", sa.String(255), nullable=True))
    op.add_column("global_contacts", sa.Column("bio_links", sa.Text(), nullable=True))
    op.add_column("global_contacts", sa.Column("external_url", sa.String(512), nullable=True))
    op.add_column("global_contacts", sa.Column("contact_source", sa.Text(), nullable=True))
    op.add_column("global_contacts", sa.Column("contact_extra", sa.Text(), nullable=True))
    op.add_column("global_contacts", sa.Column("scrape_sources", sa.Text(), nullable=False, server_default="[]"))
    op.add_column("global_contacts", sa.Column("first_seen_at", sa.DateTime(), nullable=True))

    # campaigns: messaging toggle + scrape cap override + template nullable
    op.add_column("campaigns", sa.Column("messaging_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("campaigns", sa.Column("scrape_daily_limit", sa.Integer(), nullable=True))
    op.alter_column("campaigns", "base_message_template", existing_type=sa.Text(), nullable=True)

    # instagram_accounts: daily scrape lookup counter
    op.add_column("instagram_accounts", sa.Column("scrape_lookups_today", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("instagram_accounts", "scrape_lookups_today")
    op.alter_column("campaigns", "base_message_template", existing_type=sa.Text(), nullable=False)
    op.drop_column("campaigns", "scrape_daily_limit")
    op.drop_column("campaigns", "messaging_enabled")
    for col in ("first_seen_at", "scrape_sources", "contact_extra", "contact_source",
                "external_url", "bio_links", "whatsapp", "email", "phone"):
        op.drop_column("global_contacts", col)
    for col in ("contact_extra", "contact_source", "bio_links", "whatsapp", "email", "phone"):
        op.drop_column("followers", col)
