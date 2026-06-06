"""Upsert + merge of scraped profiles into the global lead registry.

A scraped profile becomes a "lead visto" in global_contacts even when the
campaign never sends a DM (messaging disabled). The DM dedup stays at send-time
(campaign_orchestrator._mark_globally_contacted) and is unaffected.
"""
from __future__ import annotations

import json
from datetime import datetime

from loguru import logger
from sqlalchemy import select

from app.models.global_contact import GlobalContact
from app.utils.contact_extract import ContactData, SOURCE_PRIORITY


def merge_scalar(existing_val, existing_src, new_val, new_src):
    """Pick the better of two contact values by source priority."""
    if not new_val:
        return existing_val, existing_src
    if not existing_val:
        return new_val, new_src
    if SOURCE_PRIORITY.get(new_src, 0) > SOURCE_PRIORITY.get(existing_src, 0):
        return new_val, new_src
    return existing_val, existing_src


def merge_bio_links(existing: list[dict], new: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for link in (existing or []) + (new or []):
        url = (link or {}).get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "title": link.get("title")})
    return out


def merge_scrape_sources(existing: list[dict], new_entry: dict) -> list[dict]:
    out = list(existing or [])
    for e in out:
        if (e.get("campaign_id") == new_entry.get("campaign_id")
                and e.get("scraping_account_id") == new_entry.get("scraping_account_id")):
            return out
    out.append(new_entry)
    return out


def _load_json(raw, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


async def upsert_lead(
    db,
    *,
    ig_user_id: int,
    username: str | None,
    full_name: str | None,
    biography: str | None,
    contacts: ContactData,
    campaign,
    account,
) -> None:
    """Insert/merge a scraped profile as a lead. Best-effort; never raises fatally."""
    try:
        now = datetime.utcnow()
        source_entry = {
            "campaign_id": campaign.id,
            "campaign_name": campaign.name,
            "scraping_account_id": account.id if account else None,
            "scraping_account_username": account.username if account else None,
            "scraped_at": now.isoformat(),
        }
        contact = (await db.execute(
            select(GlobalContact).where(GlobalContact.ig_user_id == ig_user_id)
        )).scalar_one_or_none()

        if contact is None:
            db.add(GlobalContact(
                ig_user_id=ig_user_id,
                username=username,
                full_name=full_name,
                biography=biography,
                phone=contacts.phone,
                email=contacts.email,
                whatsapp=contacts.whatsapp,
                external_url=contacts.external_url,
                bio_links=json.dumps(contacts.bio_links) if contacts.bio_links else None,
                contact_source=json.dumps(contacts.sources) if contacts.sources else None,
                scrape_sources=json.dumps([source_entry]),
                first_seen_at=now,
                last_contacted_at=None,
                contacted_by_campaign_ids="[]",
                contact_history="[]",
            ))
            await db.commit()
            return

        # Merge into existing
        prev_src = _load_json(contact.contact_source, {})
        for field_name in ("phone", "email", "whatsapp", "external_url"):
            new_val = getattr(contacts, field_name, None)
            new_src = contacts.sources.get(field_name) if field_name != "external_url" else "ig_business"
            merged_val, merged_src = merge_scalar(
                getattr(contact, field_name), prev_src.get(field_name), new_val, new_src,
            )
            setattr(contact, field_name, merged_val)
            if merged_src:
                prev_src[field_name] = merged_src
        contact.contact_source = json.dumps(prev_src) if prev_src else None
        contact.bio_links = json.dumps(
            merge_bio_links(_load_json(contact.bio_links, []), contacts.bio_links)
        )
        contact.scrape_sources = json.dumps(
            merge_scrape_sources(_load_json(contact.scrape_sources, []), source_entry)
        )
        if username:
            contact.username = username
        if full_name:
            contact.full_name = full_name
        if biography:
            contact.biography = biography
        if contact.first_seen_at is None:
            contact.first_seen_at = now
        await db.commit()
    except Exception as e:
        logger.warning(f"[Lead] upsert_lead failed for {ig_user_id} (non-fatal): {e}")
        try:
            await db.rollback()
        except Exception:
            pass
