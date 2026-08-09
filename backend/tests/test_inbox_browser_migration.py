# backend/tests/test_inbox_browser_migration.py
"""Le 4 colonne del motore inbox browser esistono e sono tutte nullable.

Nullable non e' un dettaglio: le schede raccolte prima di questo lavoro non le
hanno, e devono restare valide.
"""
import pytest
from sqlalchemy import inspect

from app.models.follower import Follower


NUOVE = ("last_message_at", "last_message_from", "last_message_text", "source_channel")


@pytest.mark.parametrize("colonna", NUOVE)
def test_colonna_presente_e_nullable(colonna):
    col = inspect(Follower).columns[colonna]
    assert col.nullable is True, f"{colonna} deve essere nullable: le schede vecchie non ce l'hanno"


def test_le_colonne_preesistenti_non_sono_state_toccate():
    cols = inspect(Follower).columns
    assert cols["ig_user_id"].nullable is False
    assert cols["username"].nullable is False
    assert cols["full_name"].nullable is True
