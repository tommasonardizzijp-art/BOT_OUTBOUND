"""Lead qualification: colonne snapshot mancanti su lead_qualification_runs.

Il modello LeadQualificationRun salva uno snapshot di target/regole/soglie al
momento della creazione (target_name, target_description, compiled_rules,
pass/reject threshold, ai_review_min/max_score) ma la 015 aveva creato la tabella
senza queste colonne -> drift modello/DB che faceva fallire l'INSERT della run.
Tabella vuota in produzione: aggiunta diretta con server_default di sicurezza.

Revision ID: 017
Revises: 016
Create Date: 2026-06-09
"""
from alembic import op
import sqlalchemy as sa

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("lead_qualification_runs", sa.Column("target_name", sa.String(255), nullable=False, server_default=""))
    op.add_column("lead_qualification_runs", sa.Column("target_description", sa.Text(), nullable=False, server_default=""))
    op.add_column("lead_qualification_runs", sa.Column("compiled_rules", sa.Text(), nullable=False, server_default="{}"))
    op.add_column("lead_qualification_runs", sa.Column("pass_threshold", sa.Integer(), nullable=False, server_default="80"))
    op.add_column("lead_qualification_runs", sa.Column("reject_threshold", sa.Integer(), nullable=False, server_default="25"))
    op.add_column("lead_qualification_runs", sa.Column("ai_review_min_score", sa.Integer(), nullable=False, server_default="26"))
    op.add_column("lead_qualification_runs", sa.Column("ai_review_max_score", sa.Integer(), nullable=False, server_default="79"))


def downgrade() -> None:
    op.drop_column("lead_qualification_runs", "ai_review_max_score")
    op.drop_column("lead_qualification_runs", "ai_review_min_score")
    op.drop_column("lead_qualification_runs", "reject_threshold")
    op.drop_column("lead_qualification_runs", "pass_threshold")
    op.drop_column("lead_qualification_runs", "compiled_rules")
    op.drop_column("lead_qualification_runs", "target_description")
    op.drop_column("lead_qualification_runs", "target_name")
