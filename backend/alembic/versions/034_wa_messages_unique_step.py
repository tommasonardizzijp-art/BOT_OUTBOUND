"""wa_messages: indice unico parziale su (campaign_id, contact_id, step_index)

Revision ID: 034
Revises: 033
Create Date: 2026-08-13

AVVIO 12/08 §5: la rete di sicurezza del fix anti-doppione non era mai stata
messa a livello DB -- l'unica difesa contro un secondo invio sullo stesso
step era il controllo applicativo in wa_sender.py (che marca 'skipped' con
'invio_gia_registrato' se esiste gia' una riga 'sending'/'sent' per lo
stesso step).

NON e' una UniqueConstraint piena sulla tripla, di proposito: wa_sender.py
(righe ~347-364) permette ESPLICITAMENTE una nuova riga WaMessage quando la
precedente per lo stesso (campaign_id, contact_id, step_index) e' 'failed'
o 'skipped' -- e' il percorso di retry (un invio fallito prima di scrivere
nel composer non deve bloccare il tentativo successivo). Una constraint piena
romperebbe quel retry alla prima riga 'failed' seguita da un secondo
tentativo. L'indice unico PARZIALE (solo status IN ('sending','sent'))
rispecchia esattamente l'invariante che il codice applicativo gia' cerca di
garantire, senza toccare il caso dei retry.

Verificato PRIMA di scrivere questa migrazione (read-only, 13/08): 124
wa_messages totali in produzione, tutti 'sent', zero gruppi duplicati anche
sulla tripla piena -- si applica senza backfill in entrambi i casi, ma solo
la versione parziale resta corretta quando arrivera' il primo retry vero.
"""
import sqlalchemy as sa
from alembic import op

revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None

_NOME_INDICE = "uq_wa_messages_campaign_contact_step_attivo"


def upgrade() -> None:
    op.create_index(
        _NOME_INDICE,
        "wa_messages",
        ["campaign_id", "contact_id", "step_index"],
        unique=True,
        postgresql_where=sa.text("status IN ('sending', 'sent')"),
        sqlite_where=sa.text("status IN ('sending', 'sent')"),
    )


def downgrade() -> None:
    op.drop_index(_NOME_INDICE, table_name="wa_messages")
