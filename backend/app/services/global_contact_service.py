"""Upsert + merge of scraped profiles into the global lead registry.

A scraped profile becomes a "lead visto" in global_contacts even when the
campaign never sends a DM (messaging disabled). The DM dedup stays at send-time
(campaign_orchestrator._mark_globally_contacted) and is unaffected.
"""
from __future__ import annotations

import json
from datetime import datetime

from loguru import logger
from sqlalchemy import or_, select

from app.models.global_contact import GlobalContact
from app.services.inbox_browser.targa import e_provvisoria, handle_valido, normalizza_username
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


def targa_ammessa_in_anagrafica(ig_user_id: int | None) -> bool:
    """L'anagrafica accetta sia i pk reali sia le targhe derivate dallo username.

    Storia di questa funzione, perche' il ribaltamento sia leggibile: rifiutava le
    targhe negative (provvisorie) per non far entrare in anagrafica una chiave che
    la stessa persona vista dal canale API non avrebbe riconosciuto. Il costo era
    che `reservation.try_reserve` ritornava False e il contatto finiva `skipped`
    con motivo "already_contacted_globally": non una protezione contro il doppio
    DM, ma un DM che non partiva mai.

    Oggi il ponte fra le due rappresentazioni e' `global_contacts.username_norm`
    (UNIQUE, migration 039): la stessa persona converge su una riga sola
    qualunque canale l'abbia vista, quindi la targa negativa non spacca piu'
    nulla. Resta escluso solo cio' che non e' una targa: None e zero.

    I segnaposto dei profili chiusi non arrivano fin qui: li ferma `handle_valido`
    in ingresso (inbox_browser/targa.py, applicato in scrape_inbox.py).
    """
    return ig_user_id is not None and ig_user_id != 0


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
    if not targa_ammessa_in_anagrafica(ig_user_id):
        # Da Task 5 le targhe provvisorie sono ammesse: qui ci finisce solo cio'
        # che non e' una targa (None o zero). Il messaggio vecchio parlava di
        # "identificativo provvisorio ... non ancora arricchito" e da oggi
        # descriverebbe una causa che non esiste piu'.
        logger.warning(
            f"[GlobalContact] @{username}: identificativo assente o nullo "
            f"({ig_user_id}), lead non registrato in anagrafica"
        )
        return
    try:
        now = datetime.utcnow()
        source_entry = {
            "campaign_id": campaign.id,
            "campaign_name": campaign.name,
            "scraping_account_id": account.id if account else None,
            "scraping_account_username": account.username if account else None,
            "scraped_at": now.isoformat(),
        }
        # Ponte fra le due rappresentazioni della stessa persona (migration 039):
        # pk reale (canale API) e targa provvisoria (canale browser). I segnaposto
        # dei profili chiusi non hanno la forma di un handle (handle_valido) e
        # restano fuori dalla chiave: username_norm resta NULL per loro.
        username_norm = normalizza_username(username) if handle_valido(username) else None

        conditions = [GlobalContact.ig_user_id == ig_user_id]
        if username_norm:
            conditions.append(GlobalContact.username_norm == username_norm)
        contact = (await db.execute(
            select(GlobalContact).where(or_(*conditions))
        )).scalars().first()

        if contact is None:
            db.add(GlobalContact(
                ig_user_id=ig_user_id,
                username=username,
                username_norm=username_norm,
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

        # La riga trovata via username_norm porta ancora la targa provvisoria
        # del canale browser, e adesso arriva un pk reale (canale API, o
        # l'harvest post-invio che l'ha appena ancorata): promuove la riga
        # invece di lasciarne nascere una seconda con la stessa persona.
        if e_provvisoria(contact.ig_user_id) and not e_provvisoria(ig_user_id):
            logger.info(
                f"[GlobalContact] @{username}: promuovo la targa provvisoria "
                f"({contact.ig_user_id}) al pk reale ({ig_user_id})"
            )
            contact.ig_user_id = ig_user_id
        if username_norm:
            contact.username_norm = username_norm

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
