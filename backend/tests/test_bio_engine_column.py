"""Fase Bio: colonna bio_engine sul modello Campaign."""
from app.models.campaign import Campaign


def test_bio_engine_column_present():
    assert "bio_engine" in Campaign.__table__.columns.keys()


def test_bio_engine_default_api():
    col = Campaign.__table__.columns["bio_engine"]
    assert col.default.arg == "api"
    assert col.nullable is False
