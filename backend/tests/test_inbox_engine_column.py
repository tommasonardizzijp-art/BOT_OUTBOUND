"""Inbox: colonna inbox_engine sul modello Campaign."""
from app.models.campaign import Campaign


def test_inbox_engine_column_present():
    cols = Campaign.__table__.columns.keys()
    assert "inbox_engine" in cols


def test_inbox_engine_default_browser():
    col = Campaign.__table__.columns["inbox_engine"]
    assert col.default.arg == "browser"
    assert col.nullable is False
