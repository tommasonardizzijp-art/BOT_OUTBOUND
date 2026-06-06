from app.utils.contact_extract import (
    extract_contacts, ContactData, CONTACT_SOURCE_IG, CONTACT_SOURCE_REGEX,
)


class _Link:
    def __init__(self, url, title=None):
        self.url = url
        self.title = title


class _User:
    """Mirror dei campi instagrapi.types.User usati da extract_contacts."""
    def __init__(self, **kw):
        self.biography = kw.get("biography")
        self.public_email = kw.get("public_email")
        self.public_phone_number = kw.get("public_phone_number")
        self.public_phone_country_code = kw.get("public_phone_country_code")
        self.contact_phone_number = kw.get("contact_phone_number")
        self.external_url = kw.get("external_url")
        self.bio_links = kw.get("bio_links", [])


def test_business_full_contact():
    u = _User(
        biography="Negozio abbigliamento",
        public_email="Shop@Example.COM",
        public_phone_number="3331234567",
        public_phone_country_code="39",
        bio_links=[_Link("https://shop.example.com", "Sito")],
        external_url="https://shop.example.com",
    )
    c = extract_contacts(u)
    assert c.email == "shop@example.com"
    assert c.phone == "+393331234567"
    assert c.sources["email"] == CONTACT_SOURCE_IG
    assert c.sources["phone"] == CONTACT_SOURCE_IG
    assert {l["url"] for l in c.bio_links} == {"https://shop.example.com"}


def test_email_from_bio_text_when_no_business_field():
    u = _User(biography="Scrivimi a info@negozio.it per ordini")
    c = extract_contacts(u)
    assert c.email == "info@negozio.it"
    assert c.sources["email"] == CONTACT_SOURCE_REGEX


def test_whatsapp_link_in_bio_links():
    u = _User(
        biography="Ordina su WhatsApp",
        bio_links=[_Link("https://wa.me/393339998877")],
    )
    c = extract_contacts(u)
    assert c.whatsapp == "+393339998877"
    # whatsapp number also fills phone when phone missing
    assert c.phone == "+393339998877"


def test_phone_in_bio_text():
    u = _User(biography="Chiama +39 333 444 5566 dalle 9 alle 18")
    c = extract_contacts(u)
    assert c.phone == "+393334445566"
    assert c.sources["phone"] == CONTACT_SOURCE_REGEX


def test_no_false_phone_from_year_or_short_number():
    u = _User(biography="Dal 2024 a Milano. Sconto 20%")
    c = extract_contacts(u)
    assert c.phone is None


def test_multiple_bio_links_preserved_and_deduped():
    u = _User(bio_links=[
        _Link("https://a.com", "A"),
        _Link("https://b.com", "B"),
        _Link("https://a.com", "A dup"),
    ], external_url="https://a.com")
    c = extract_contacts(u)
    urls = [l["url"] for l in c.bio_links]
    assert urls.count("https://a.com") == 1
    assert "https://b.com" in urls


def test_business_phone_wins_over_bio_regex():
    u = _User(
        biography="vecchio numero 02 1111111",
        public_phone_number="3331234567",
        public_phone_country_code="39",
    )
    c = extract_contacts(u)
    assert c.phone == "+393331234567"
    assert c.sources["phone"] == CONTACT_SOURCE_IG


def test_empty_input_no_exception():
    c = extract_contacts(_User())
    assert isinstance(c, ContactData)
    assert c.phone is None and c.email is None and c.bio_links == []


def test_handles_none_gracefully():
    c = extract_contacts(None)
    assert isinstance(c, ContactData)
    assert c.phone is None
