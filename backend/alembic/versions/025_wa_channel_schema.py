"""Schema del canale WhatsApp: tenants + 8 tabelle wa_*.

Interamente ADDITIVA: nessun ALTER su tabelle esistenti (SDD 5.3). Il canale
Instagram in produzione non si accorge di questa migrazione.

Gli enum sono colonne sa.String con vincolo applicativo, non sa.Enum nativo
Postgres: un ENUM Postgres va alterato con ALTER TYPE a ogni valore nuovo, e
la state machine dei numeri (SDD 8.3) ne guadagnera' presto. Il modello
SQLAlchemy usa comunque SAEnum(native_enum=False), che valida lato Python.

Revision ID: 025
Revises: 024
Create Date: 2026-07-27
"""
from alembic import op
import sqlalchemy as sa

revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("settings", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )

    op.create_table(
        "wa_numbers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("phone_hmac", sa.String(64), nullable=False, unique=True),
        sa.Column("encrypted_phone", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending_qr"),
        sa.Column("browser_profile", sa.String(500), nullable=True),
        sa.Column("proxy_url", sa.String(500), nullable=True),
        sa.Column("daily_cap", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("warmup_day", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("sent_today", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_date", sa.String(10), nullable=True),
        sa.Column("session_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )

    op.create_table(
        "wa_contacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("phone_hmac", sa.String(64), nullable=False),
        sa.Column("encrypted_phone", sa.Text(), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=True),
        sa.Column("chat_title", sa.String(200), nullable=True),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("opted_out", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("opted_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("do_not_contact", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("dnc_reason", sa.String(20), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("last_contacted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_replied_at", sa.DateTime(timezone=True), nullable=True),
        # Un doppio upload dello stesso CSV non deve creare due contatti:
        # sarebbe la stessa persona contattata due volte. Vincolo inline nella
        # create_table (non un ALTER separato dopo): SQLite non supporta
        # ALTER TABLE ADD CONSTRAINT senza batch mode.
        sa.UniqueConstraint("tenant_id", "phone_hmac", name="uq_wa_contacts_tenant_phone"),
    )

    op.create_table(
        "wa_campaigns",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("wa_number_id", sa.String(36), sa.ForeignKey("wa_numbers.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("campaign_type", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("daily_limit", sa.Integer(), nullable=True),
        sa.Column("optout_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("optout_cta", sa.String(500), nullable=True),
        sa.Column("active_hours_start", sa.String(5), nullable=True),
        sa.Column("active_hours_end", sa.String(5), nullable=True),
        sa.Column("session_min_messages", sa.Integer(), nullable=True),
        sa.Column("session_max_messages", sa.Integer(), nullable=True),
        sa.Column("break_min_minutes", sa.Integer(), nullable=True),
        sa.Column("break_max_minutes", sa.Integer(), nullable=True),
        sa.Column("total_contacts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("replied", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("opted_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "wa_sequence_steps",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("campaign_id", sa.String(36), sa.ForeignKey("wa_campaigns.id"), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("template_a", sa.Text(), nullable=False),
        sa.Column("template_b", sa.Text(), nullable=True),
        sa.Column("template_c", sa.Text(), nullable=True),
        sa.Column("template_d", sa.Text(), nullable=True),
        sa.Column("send_condition", sa.String(20), nullable=False, server_default="always"),
        sa.Column("wait_days", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("campaign_id", "step_index", name="uq_wa_steps_campaign_index"),
    )

    op.create_table(
        "wa_campaign_contacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("campaign_id", sa.String(36), sa.ForeignKey("wa_campaigns.id"), nullable=False),
        sa.Column("contact_id", sa.String(36), sa.ForeignKey("wa_contacts.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("current_step", sa.Integer(), nullable=False, server_default="-1"),
        sa.Column("next_action_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replied_at_step", sa.Integer(), nullable=True),
        sa.Column("locked_by", sa.String(36), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.UniqueConstraint("campaign_id", "contact_id", name="uq_wa_camp_contact"),
    )
    # next_action_at e' la colonna su cui gira il tick del sequence engine:
    # senza indice, ogni tick e' una scansione completa della tabella.
    op.create_index("ix_wa_campaign_contacts_next_action", "wa_campaign_contacts",
                    ["next_action_at"])

    op.create_table(
        "wa_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("campaign_id", sa.String(36), sa.ForeignKey("wa_campaigns.id"), nullable=False),
        sa.Column("contact_id", sa.String(36), sa.ForeignKey("wa_contacts.id"), nullable=False),
        sa.Column("wa_number_id", sa.String(36), sa.ForeignKey("wa_numbers.id"), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("template_variant", sa.String(1), nullable=False),
        sa.Column("rendered_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivery_check", sa.String(20), nullable=True),
    )

    op.create_table(
        "wa_inbound_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("wa_number_id", sa.String(36), sa.ForeignKey("wa_numbers.id"), nullable=False),
        sa.Column("contact_id", sa.String(36), sa.ForeignKey("wa_contacts.id"), nullable=True),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("preview_text", sa.Text(), nullable=True),
        sa.Column("matched_by", sa.String(20), nullable=False),
        sa.Column("processed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    # Ordine inverso: le FK non permettono di droppare un padre prima dei figli.
    for tabella in ("wa_inbound_events", "wa_messages", "wa_campaign_contacts",
                    "wa_sequence_steps", "wa_campaigns", "wa_contacts",
                    "wa_numbers", "tenants"):
        op.drop_table(tabella)
