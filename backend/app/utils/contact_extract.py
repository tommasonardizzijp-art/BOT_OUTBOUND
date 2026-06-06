"""Pure contact extraction from an instagrapi user_info object.

Single source of truth used by both the scraper and the import resolver, so
the two paths never diverge (CLAUDE.md anti-divergence rule). No DB/network.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

CONTACT_SOURCE_IG = "ig_business"
CONTACT_SOURCE_REGEX = "bio_regex"
CONTACT_SOURCE_WEBSITE = "website"  # reserved for Fase 2

# Priority for cross-source merges (higher wins).
SOURCE_PRIORITY = {CONTACT_SOURCE_IG: 3, CONTACT_SOURCE_REGEX: 2, CONTACT_SOURCE_WEBSITE: 1}

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Phone candidate: optional +, then digits/separators, 8+ chars, ends on a digit.
_PHONE_RE = re.compile(r"(?<![\w.])(\+?\d[\d\s().\-/]{6,}\d)(?![\w])")
_WA_NUM_RE = re.compile(r"(?:wa\.me/|api\.whatsapp\.com/send\?phone=)\+?(\d{6,15})", re.I)
_WA_GROUP_RE = re.compile(r"(https?://chat\.whatsapp\.com/[A-Za-z0-9]+)", re.I)


@dataclass
class ContactData:
    phone: str | None = None
    email: str | None = None
    whatsapp: str | None = None
    bio_links: list[dict] = field(default_factory=list)
    external_url: str | None = None
    sources: dict = field(default_factory=dict)


def _normalize_email(raw: str | None) -> str | None:
    if not raw:
        return None
    s = str(raw).strip().lower()
    return s if _EMAIL_RE.fullmatch(s) else (s if "@" in s and "." in s else None)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _normalize_phone(raw: str | None) -> str | None:
    """Return E.164-ish '+<digits>' or '<digits>'. None if implausible length."""
    if not raw:
        return None
    s = str(raw).strip()
    plus = s.startswith("+")
    d = _digits(s)
    if not (9 <= len(d) <= 15):
        return None
    return ("+" + d) if plus else d


def _compose_business_phone(number: str | None, cc: str | None) -> str | None:
    num_d = _digits(number or "")
    if not num_d:
        return None
    cc_d = _digits(cc or "")
    if cc_d:
        return "+" + cc_d + num_d.lstrip("0")
    # No country code: accept the number as-is if plausible length.
    return _normalize_phone(num_d)


def _emails_from_text(text: str | None) -> str | None:
    if not text:
        return None
    m = _EMAIL_RE.search(text)
    return m.group(0).lower() if m else None


def _phones_from_text(text: str | None) -> str | None:
    if not text:
        return None
    for m in _PHONE_RE.finditer(text):
        normalized = _normalize_phone(m.group(1))
        if normalized:
            return normalized
    return None


def _whatsapp_from(texts: list[str]) -> tuple[str | None, str | None]:
    """Return (whatsapp_value, phone_from_whatsapp)."""
    for t in texts:
        if not t:
            continue
        m = _WA_NUM_RE.search(t)
        if m:
            # wa.me/api numbers are always international — force E.164 '+' prefix.
            phone = "+" + _digits(m.group(1))
            return phone, phone
        g = _WA_GROUP_RE.search(t)
        if g:
            return g.group(1), None
    return None, None


def _bio_links_from(info) -> tuple[list[dict], str | None]:
    links: list[dict] = []
    seen: set[str] = set()

    def _add(url, title):
        if not url:
            return
        u = str(url).strip()
        if not u or u in seen:
            return
        seen.add(u)
        links.append({"url": u, "title": (str(title).strip() if title else None)})

    raw_links = getattr(info, "bio_links", None) or []
    for bl in raw_links:
        if isinstance(bl, dict):
            _add(bl.get("url"), bl.get("title") or bl.get("link_type"))
        else:
            _add(getattr(bl, "url", None), getattr(bl, "title", None))

    external = getattr(info, "external_url", None)
    external = str(external) if external else None
    if external:
        _add(external, None)
    return links, external


def extract_contacts(info) -> ContactData:
    """Extract contacts from an instagrapi User (or None). Never raises."""
    data = ContactData()
    if info is None:
        return data
    try:
        bio = getattr(info, "biography", None) or ""
        bio_links, external = _bio_links_from(info)
        data.bio_links = bio_links
        data.external_url = external
        link_urls = [l["url"] for l in bio_links]

        # ── Email ──
        biz_email = _normalize_email(getattr(info, "public_email", None))
        if biz_email:
            data.email = biz_email
            data.sources["email"] = CONTACT_SOURCE_IG
        else:
            re_email = _emails_from_text(bio)
            if re_email:
                data.email = re_email
                data.sources["email"] = CONTACT_SOURCE_REGEX

        # ── WhatsApp (links + bio text) ──
        wa, wa_phone = _whatsapp_from(link_urls + [bio])
        if wa:
            data.whatsapp = wa
            data.sources["whatsapp"] = CONTACT_SOURCE_REGEX

        # ── Phone ──
        biz_phone = _compose_business_phone(
            getattr(info, "public_phone_number", None)
            or getattr(info, "contact_phone_number", None),
            getattr(info, "public_phone_country_code", None),
        )
        if biz_phone:
            data.phone = biz_phone
            data.sources["phone"] = CONTACT_SOURCE_IG
        elif wa_phone:
            data.phone = wa_phone
            data.sources["phone"] = CONTACT_SOURCE_REGEX
        else:
            re_phone = _phones_from_text(bio)
            if re_phone:
                data.phone = re_phone
                data.sources["phone"] = CONTACT_SOURCE_REGEX
    except Exception:
        # Defensive: bad input must never break a scrape.
        return data
    return data
