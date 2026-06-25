"""Inbox: colonna inbox_engine sul modello Campaign."""
from app.models.campaign import Campaign


def test_inbox_engine_column_present():
    cols = Campaign.__table__.columns.keys()
    assert "inbox_engine" in cols


def test_inbox_engine_default_api():
    # Default 'api': lo scraping inbox via browser e' stato rimosso (vedi migration 020).
    col = Campaign.__table__.columns["inbox_engine"]
    assert col.default.arg == "api"
    assert col.nullable is False
