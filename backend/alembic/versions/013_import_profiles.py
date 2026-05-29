"""Import profiles: source_type on campaigns + imported_profiles staging table.

Revision ID: 013
Revises: 012
Create Date: 2026-05-29
"""
from alembic import op
import sqlalchemy as sa

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column("source_type", sa.String(20), nullable=False, server_default="scrape"),
    )
    # target_username was NOT NULL; for import campaigns there is no target page.
    op.alter_column("campaigns", "target_username", existing_type=sa.String(255), nullable=True)

    op.create_table(
        "imported_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("campaign_id", sa.String(36), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("raw_input", sa.String(512), nullable=False),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("ig_user_id", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("campaign_id", "username", name="uq_import_campaign_username"),
    )
    op.create_index("idx_imported_profiles_campaign", "imported_profiles", ["campaign_id", "status"])


def downgrade() -> None:
    op.drop_index("idx_imported_profiles_campaign", table_name="imported_profiles")
    op.drop_table("imported_profiles")
    op.alter_column("campaigns", "target_username", existing_type=sa.String(255), nullable=False)
    op.drop_column("campaigns", "source_type")
