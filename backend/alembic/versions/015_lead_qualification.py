"""Lead qualification profiles, runs and results.

Revision ID: 015
Revises: 014
Create Date: 2026-06-08
"""
from alembic import op
import sqlalchemy as sa

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lead_target_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("compiled_rules", sa.Text(), nullable=False),
        sa.Column("rules_hash", sa.String(64), nullable=False),
        sa.Column("pass_threshold", sa.Integer(), nullable=False, server_default="80"),
        sa.Column("reject_threshold", sa.Integer(), nullable=False, server_default="25"),
        sa.Column("ai_review_min_score", sa.Integer(), nullable=False, server_default="26"),
        sa.Column("ai_review_max_score", sa.Integer(), nullable=False, server_default="79"),
        sa.Column("max_run_size", sa.Integer(), nullable=False, server_default="5000"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_lead_target_profiles_created_at", "lead_target_profiles", ["created_at"])

    op.create_table(
        "lead_qualification_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "target_profile_id",
            sa.String(36),
            sa.ForeignKey("lead_target_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("target_name", sa.String(255), nullable=False),
        sa.Column("target_description", sa.Text(), nullable=False),
        sa.Column("compiled_rules", sa.Text(), nullable=False),
        sa.Column("filters", sa.Text(), nullable=False),
        sa.Column("rules_hash", sa.String(64), nullable=False),
        sa.Column("pass_threshold", sa.Integer(), nullable=False, server_default="80"),
        sa.Column("reject_threshold", sa.Integer(), nullable=False, server_default="25"),
        sa.Column("ai_review_min_score", sa.Integer(), nullable=False, server_default="26"),
        sa.Column("ai_review_max_score", sa.Integer(), nullable=False, server_default="79"),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("total_candidates", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_existing", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("matched_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("no_match_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ambiguous_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ai_reviewed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_lq_runs_profile_status", "lead_qualification_runs", ["target_profile_id", "status"])
    op.create_index("ix_lq_runs_created_at", "lead_qualification_runs", ["created_at"])

    op.create_table(
        "lead_qualifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "global_contact_id",
            sa.String(36),
            sa.ForeignKey("global_contacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ig_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "target_profile_id",
            sa.String(36),
            sa.ForeignKey("lead_target_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("lead_qualification_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rules_hash", sa.String(64), nullable=False),
        sa.Column("deterministic_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ai_score", sa.Integer(), nullable=True),
        sa.Column("final_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("matched_signals", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("negative_signals", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("ai_used", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ai_label", sa.String(255), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("model_used", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("run_id", "global_contact_id", name="uq_lq_run_contact"),
    )
    op.create_index(
        "ix_lq_target_contact_rules",
        "lead_qualifications",
        ["target_profile_id", "global_contact_id", "rules_hash"],
    )
    op.create_index(
        "ix_lq_target_status_score",
        "lead_qualifications",
        ["target_profile_id", "status", "final_score"],
    )
    op.create_index("ix_lq_run", "lead_qualifications", ["run_id"])
    op.create_index("ix_lq_ig_user_id", "lead_qualifications", ["ig_user_id"])


def downgrade() -> None:
    op.drop_index("ix_lq_ig_user_id", table_name="lead_qualifications")
    op.drop_index("ix_lq_run", table_name="lead_qualifications")
    op.drop_index("ix_lq_target_status_score", table_name="lead_qualifications")
    op.drop_index("ix_lq_target_contact_rules", table_name="lead_qualifications")
    op.drop_table("lead_qualifications")
    op.drop_index("ix_lq_runs_created_at", table_name="lead_qualification_runs")
    op.drop_index("ix_lq_runs_profile_status", table_name="lead_qualification_runs")
    op.drop_table("lead_qualification_runs")
    op.drop_index("ix_lead_target_profiles_created_at", table_name="lead_target_profiles")
    op.drop_table("lead_target_profiles")
