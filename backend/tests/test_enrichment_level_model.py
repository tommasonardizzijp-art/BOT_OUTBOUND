"""Il livello di arricchimento decide SE si arricchisce; bio_engine decide COME.
Sono assi indipendenti e non devono essere confusi."""
from app.models.campaign import (
    Campaign, ENRICHMENT_NONE, ENRICHMENT_BIO, ENRICHMENT_CONTACTS, ENRICHMENT_LEVELS,
)


def test_livelli_dichiarati():
    assert ENRICHMENT_LEVELS == (ENRICHMENT_NONE, ENRICHMENT_BIO, ENRICHMENT_CONTACTS)
    assert (ENRICHMENT_NONE, ENRICHMENT_BIO, ENRICHMENT_CONTACTS) == ("none", "bio", "contacts")


def test_default_none_sulle_campagne_nuove():
    # SQLAlchemy applica i default di colonna solo al flush, non al costruttore
    # (stesso pattern gia' noto in questa codebase, vedi
    # test_template_mode_schema.py::test_campaign_model_defaults per ai_enabled):
    # per verificare il default reale serve un flush vero, non solo l'oggetto
    # appena costruito -- altrimenti l'assert passerebbe anche con un default
    # sbagliato dichiarato sulla colonna. Engine sqlite in-memory dedicato:
    # Campaign non ha ForeignKey, non serve lo schema completo dell'app.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.database import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Campaign.__table__])
    Session = sessionmaker(bind=engine)
    with Session() as s:
        c = Campaign(name="test")
        s.add(c)
        s.flush()
        assert c.enrichment_level == ENRICHMENT_NONE


def test_il_livello_e_indipendente_dal_motore():
    c = Campaign(name="test", enrichment_level=ENRICHMENT_CONTACTS, bio_engine="api")
    assert c.enrichment_level == ENRICHMENT_CONTACTS
    assert c.bio_engine == "api"
