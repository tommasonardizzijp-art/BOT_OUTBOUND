"""Fix campaign column types.

Revision ID: 008
Revises: 007
Create Date: 2026-05-18
"""
from alembic import op
import sqlalchemy as sa


revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def _pg_column_type(bind, column_name: str) -> str | None:
    return bind.execute(
        sa.text(
            """
            SELECT data_type
            FROM information_schema.columns
            WHERE table_name = 'campaigns'
              AND column_name = :column_name
              AND table_schema = current_schema()
            """
        ),
        {"column_name": column_name},
    ).scalar()


def upgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name == "postgresql":
        if _pg_column_type(bind, "bio_fetch_delay_min") != "double precision":
            op.alter_column(
                "campaigns",
                "bio_fetch_delay_min",
                type_=sa.Float(),
                existing_type=sa.Integer(),
                existing_server_default=sa.text("5"),
                server_default=sa.text("5.0"),
                postgresql_using="bio_fetch_delay_min::double precision",
            )
        if _pg_column_type(bind, "bio_fetch_delay_max") != "double precision":
            op.alter_column(
                "campaigns",
                "bio_fetch_delay_max",
                type_=sa.Float(),
                existing_type=sa.Integer(),
                existing_server_default=sa.text("8"),
                server_default=sa.text("8.0"),
                postgresql_using="bio_fetch_delay_max::double precision",
            )
        if _pg_column_type(bind, "require_approval") != "boolean":
            op.alter_column("campaigns", "require_approval", server_default=None)
            op.alter_column(
                "campaigns",
                "require_approval",
                type_=sa.Boolean(),
                existing_type=sa.Integer(),
                server_default=sa.false(),
                postgresql_using="require_approval::integer::boolean",
            )
        if _pg_column_type(bind, "auto_generate") != "boolean":
            op.alter_column("campaigns", "auto_generate", server_default=None)
            op.alter_column(
                "campaigns",
                "auto_generate",
                type_=sa.Boolean(),
                existing_type=sa.Integer(),
                server_default=sa.false(),
                postgresql_using="auto_generate::integer::boolean",
            )
        return

    # SQLite: type affinity is dynamic; keep data intact.


def downgrade() -> None:
    pass
