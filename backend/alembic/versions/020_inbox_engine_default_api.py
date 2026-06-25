"""Inbox DM scraping: default engine -> 'api', backfill 'browser' -> 'api'.

Lo scraping inbox via browsing del DOM e' stato rimosso (la lista DM su
Instagram web mostra solo il nome visualizzato, non username/pk: nessun
identificatore estraibile). Il backend usa sempre l'API. Allineiamo il default
della colonna e convertiamo le campagne esistenti 'browser' -> 'api' cosi'
girano sull'engine funzionante.

Revision ID: 020
Revises: 019
Create Date: 2026-06-25
"""
from alembic import op
import sqlalchemy as sa

revision = "020"
down_revision = "019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # nuovo default lato DB
    op.alter_column("campaigns", "inbox_engine", server_default="api")
    # backfill righe esistenti: 'browser' non funziona piu' -> 'api'
    op.execute("UPDATE campaigns SET inbox_engine = 'api' WHERE inbox_engine = 'browser'")


def downgrade() -> None:
    op.alter_column("campaigns", "inbox_engine", server_default="browser")
