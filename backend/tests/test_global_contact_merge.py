from app.services.global_contact_service import merge_scalar, merge_bio_links, merge_scrape_sources
from app.utils.contact_extract import CONTACT_SOURCE_IG, CONTACT_SOURCE_REGEX


def test_merge_scalar_fills_empty():
    val, src = merge_scalar(None, None, "+39333", CONTACT_SOURCE_REGEX)
    assert val == "+39333" and src == CONTACT_SOURCE_REGEX


def test_merge_scalar_keeps_existing_same_priority():
    val, src = merge_scalar("+39111", CONTACT_SOURCE_REGEX, "+39222", CONTACT_SOURCE_REGEX)
    assert val == "+39111" and src == CONTACT_SOURCE_REGEX


def test_merge_scalar_higher_priority_overrides():
    val, src = merge_scalar("+39111", CONTACT_SOURCE_REGEX, "+39222", CONTACT_SOURCE_IG)
    assert val == "+39222" and src == CONTACT_SOURCE_IG


def test_merge_scalar_new_none_keeps_existing():
    val, src = merge_scalar("+39111", CONTACT_SOURCE_IG, None, None)
    assert val == "+39111" and src == CONTACT_SOURCE_IG


def test_merge_bio_links_union_dedup():
    existing = [{"url": "https://a.com", "title": "A"}]
    new = [{"url": "https://a.com", "title": "A2"}, {"url": "https://b.com", "title": "B"}]
    merged = merge_bio_links(existing, new)
    urls = [l["url"] for l in merged]
    assert urls == ["https://a.com", "https://b.com"]


def test_merge_scrape_sources_appends_unique():
    existing = [{"campaign_id": "c1", "scraping_account_id": "a1"}]
    new_entry = {"campaign_id": "c1", "scraping_account_id": "a1"}
    assert merge_scrape_sources(existing, new_entry) == existing  # no dup
    other = {"campaign_id": "c2", "scraping_account_id": "a1"}
    assert len(merge_scrape_sources(existing, other)) == 2
