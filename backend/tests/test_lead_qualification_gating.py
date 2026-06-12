"""Lead qualification scoring — recall-first design.

Filosofia (richiesta dall'utente): basta UNA keyword di nicchia corretta perche'
il lead sia MATCH, senza passare dall'AI. L'AI interviene solo sui segnali deboli
(parole generiche). Nessuna parola negativa: non vogliamo falsi negativi, il
cliente filtra a valle.

Gate:
  score >= pass_threshold(10) -> match   (1 keyword specifica basta)
  0 < score <  pass            -> ambiguous -> AI   (generiche/deboli)
  score == 0                   -> no_match (nessun segnale)
"""
from types import SimpleNamespace

from app.models.lead_qualification import LeadTargetProfile
from app.schemas.lead_qualification import LeadThresholdsMixin
from app.services.lead_qualification import score_lead

RULES = {
    "target_label": "fashion_clothing",
    # specifiche -> match diretto
    "positive_terms": ["abbigliamento", "moda", "clothing", "clothes", "boutique",
                       "vestiti", "fashion", "negozio", "brand", "style"],
    "strong_terms": ["abbigliamento donna", "fashion brand"],
    # generiche ma possibili -> fascia AI
    "positive_concepts": ["uomo", "donna"],
    # niente negativi
    "negative_terms": [],
    "negative_concepts": [],
}


def _lead(**kw):
    base = dict(username="shop", full_name="", biography="", external_url=None,
                bio_links=None, phone=None, email=None, whatsapp=None, scrape_sources=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _th():
    return dict(
        pass_threshold=LeadTargetProfile.pass_threshold.default.arg,
        reject_threshold=LeadTargetProfile.reject_threshold.default.arg,
    )


def _score(contact):
    t = _th()
    return score_lead(contact, RULES, pass_threshold=t["pass_threshold"], reject_threshold=t["reject_threshold"])


def test_single_specific_keyword_is_match():
    res = _score(_lead(biography="Articoli sportivi. Abbigliamento casual"))
    assert res.status == "match", f"single niche keyword not match, score={res.score}"


def test_specific_keyword_on_username_is_match():
    # field_weight piu' basso: deve comunque superare 10.
    res = _score(_lead(username="moda_milano", biography=""))
    assert res.status == "match", f"keyword on username not match, score={res.score}"


def test_keyword_inside_handle_is_match():
    res = _score(_lead(biography="Testa, mani e cuore di @hanami_clothing"))
    assert res.status == "match", f"keyword inside handle not match, score={res.score}"


def test_good_indicator_word_is_match():
    # "negozio"/"brand"/"style": l'utente li considera ottimi indicatori -> match.
    res = _score(_lead(biography="Il mio negozio nel centro di Roma"))
    assert res.status == "match", f"good-indicator word not match, score={res.score}"


def test_generic_word_goes_to_ai():
    # "uomo" da solo: generico -> ambiguous, dentro la finestra AI.
    th = LeadThresholdsMixin()
    res = _score(_lead(biography="Ciao sono un uomo semplice e sportivo"))
    assert res.status == "ambiguous", f"generic word not routed to AI, status={res.status} score={res.score}"
    assert th.ai_review_min_score <= res.score <= th.ai_review_max_score, (
        f"score {res.score} outside AI window [{th.ai_review_min_score},{th.ai_review_max_score}]"
    )


def test_contact_only_without_keyword_is_no_match():
    # Telefono ma nessuna keyword di nicchia -> no_match, NON va all'AI.
    res = _score(_lead(biography="Wedding & Event Planner", phone="3491234567"))
    assert res.status == "no_match", f"contact-only sent to AI, score={res.score}"


def test_weak_concept_plus_contact_goes_to_ai():
    # Solo concept generico ("uomo") + contatto: concept(5)+contact(4)=9 < 10
    # -> ambiguous -> AI (non match diretto, ma non scartato).
    res = _score(_lead(biography="Cose da uomo, spedizioni Italia", phone="349"))
    assert res.status == "ambiguous", f"weak concept not routed to AI, score={res.score}"


def test_zero_signal_is_no_match():
    res = _score(_lead(biography="Travel backpacker. Voglia di vedere il mondo"))
    assert res.status == "no_match"


def test_campaign_name_in_scrape_source_does_not_match():
    # Bio vuota ma scrapata da una campagna chiamata "Shop survivor": "shop" NON
    # deve matchare il metadato sorgente -> no_match.
    import json
    src = json.dumps([{"campaign_name": "Scraping Shop survivor x AV", "scraping_account_username": "acc"}])
    res = _score(_lead(biography="", scrape_sources=src))
    assert res.status == "no_match", f"campaign-name leaked into match, score={res.score}"


def test_match_on_contact_option_forces_match():
    # Opzione attiva: chi ha un contatto = match, anche senza keyword.
    t = _th()
    res = score_lead(_lead(biography="Wedding planner", phone="349"),
                     RULES, pass_threshold=t["pass_threshold"], reject_threshold=t["reject_threshold"],
                     match_on_contact=True)
    assert res.status == "match", f"contact present but not matched, score={res.score}"


def test_match_on_contact_option_no_contact_falls_through():
    # Opzione attiva ma nessun contatto -> comportamento normale (no_match).
    t = _th()
    res = score_lead(_lead(biography="Travel blogger"),
                     RULES, pass_threshold=t["pass_threshold"], reject_threshold=t["reject_threshold"],
                     match_on_contact=True)
    assert res.status == "no_match"


def test_match_on_contact_recovers_phone_in_bio():
    # Nessuna colonna contatto, ma numero scritto nella bio -> match recuperato.
    t = _th()
    res = score_lead(_lead(biography="Spedizioni in tutta Italia 📲 349 251 5481"),
                     RULES, pass_threshold=t["pass_threshold"], reject_threshold=t["reject_threshold"],
                     match_on_contact=True)
    assert res.status == "match", f"phone-in-bio not recovered, score={res.score}"


def test_match_on_contact_recovers_email_in_bio():
    t = _th()
    res = score_lead(_lead(biography="Per info scrivi a vendite@negozio.it"),
                     RULES, pass_threshold=t["pass_threshold"], reject_threshold=t["reject_threshold"],
                     match_on_contact=True)
    assert res.status == "match"


def test_match_on_contact_link_counts():
    # Anche un link in bio (external_url) conta come contatto.
    t = _th()
    res = score_lead(_lead(biography="Solo citazioni", external_url="https://linktr.ee/x"),
                     RULES, pass_threshold=t["pass_threshold"], reject_threshold=t["reject_threshold"],
                     match_on_contact=True)
    assert res.status == "match"


def test_former_negative_word_does_not_reject():
    # Un lead di abbigliamento che cita anche "immobiliare" NON deve essere scartato.
    res = _score(_lead(biography="Vendo abbigliamento donna. Ex agente immobiliare"))
    assert res.status == "match", f"former-negative word penalised the lead, score={res.score}"
