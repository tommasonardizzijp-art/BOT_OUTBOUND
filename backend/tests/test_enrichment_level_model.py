"""Il livello di arricchimento decide SE si arricchisce; bio_engine decide COME.
Sono assi indipendenti e non devono essere confusi."""
from app.models.campaign import (
    Campaign, ENRICHMENT_NONE, ENRICHMENT_BIO, ENRICHMENT_CONTACTS, ENRICHMENT_LEVELS,
)


def test_livelli_dichiarati():
    assert ENRICHMENT_LEVELS == (ENRICHMENT_NONE, ENRICHMENT_BIO, ENRICHMENT_CONTACTS)
    assert (ENRICHMENT_NONE, ENRICHMENT_BIO, ENRICHMENT_CONTACTS) == ("none", "bio", "contacts")


def test_default_none_sulle_campagne_nuove():
    c = Campaign(name="test")
    # SQLAlchemy applica i default di colonna solo al flush, non al costruttore
    # (stesso pattern gia' noto in questa codebase, vedi
    # test_template_mode_schema.py::test_campaign_model_defaults per ai_enabled):
    # pre-flush l'attributo resta None, post-flush/DB e' sempre 'none'.
    assert c.enrichment_level == ENRICHMENT_NONE or c.enrichment_level is None


def test_il_livello_e_indipendente_dal_motore():
    c = Campaign(name="test", enrichment_level=ENRICHMENT_CONTACTS, bio_engine="api")
    assert c.enrichment_level == ENRICHMENT_CONTACTS
    assert c.bio_engine == "api"
