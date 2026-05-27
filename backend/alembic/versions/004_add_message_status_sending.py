"""Add MessageStatus.sending for duplicate-DM defense.

Revision ID: 004
Revises: 003
Create Date: 2026-05-08

SQLite does not support ALTER TYPE. Since the MessageStatus column uses a
VARCHAR with a CHECK constraint enforced only by SQLAlchemy at the ORM level
(not at the DB level in SQLite), the migration is a no-op for the schema itself.
The new enum value 'sending' is valid in the existing VARCHAR column and will
be written/read correctly. This migration exists for Alembic version tracking.
"""
from alembic import op
import sqlalchemy as sa

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite stores enums as VARCHAR without a DB-level CHECK constraint
    # (SQLAlchemy enforces the enum at ORM level). Adding 'sending' to the
    # Python enum is sufficient; no DDL change is required.
    #
    # If you are using PostgreSQL, replace this with:
    #   op.execute("ALTER TYPE messagestatus ADD VALUE 'sending' AFTER 'pending'")
    pass


def downgrade() -> None:
    # Remove all rows with status='sending' before downgrading to prevent
    # integrity errors when the enum value is removed from the model.
    op.execute("UPDATE messages SET status='retry' WHERE status='sending'")
