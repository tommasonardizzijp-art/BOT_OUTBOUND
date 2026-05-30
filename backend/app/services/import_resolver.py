"""Import resolver: turn imported profile lines into bio-scraped Followers.

Reuses the scraper's account selection + instagrapi login. Resolution itself
uses user_info_by_username_v1 (1 call → pk + full bio).
"""
import uuid
from datetime import datetime
from loguru import logger
from sqlalchemy import select
from instagrapi.exceptions import UserNotFound

from app.models.imported_profile import ImportedProfile
from app.utils.ig_username import parse_lines


def classify_resolution(user_info, error) -> tuple[str, bool]:
    """Map an IG resolution outcome → (staging_status, should_create_follower).

    - success public  → ('resolved', True)
    - success private → ('private', True)   # Follower comunque creato
    - UserNotFound    → ('not_found', False)
    - other exception → ('error', False)
    """
    if error is not None:
        if isinstance(error, UserNotFound):
            return "not_found", False
        return "error", False
    if getattr(user_info, "is_private", False):
        return "private", True
    return "resolved", True


async def store_imported_lines(db, campaign_id: str, raw: str) -> dict:
    """Parse a file blob and insert pending ImportedProfile rows.
    Returns counts; raises ValueError if zero valid lines."""
    parsed = parse_lines(raw)
    if not parsed["valid"]:
        raise ValueError("Nessun profilo valido trovato nel file.")

    # Dedup contro righe già presenti per questa campagna
    existing = await db.execute(
        select(ImportedProfile.username).where(ImportedProfile.campaign_id == campaign_id)
    )
    existing_usernames = {r[0] for r in existing.all()}

    inserted = 0
    skipped_existing = 0
    for username, raw_line in parsed["valid"]:
        if username in existing_usernames:
            skipped_existing += 1
            continue
        db.add(ImportedProfile(
            id=str(uuid.uuid4()),
            campaign_id=campaign_id,
            raw_input=raw_line[:512],
            username=username,
            status="pending",
        ))
        inserted += 1
    await db.commit()
    logger.info(f"[Import] Campaign {campaign_id}: {inserted} profili inseriti, "
                f"{parsed['duplicates']} duplicati file, {skipped_existing} già presenti, "
                f"{parsed['skipped_invalid']} righe scartate")
    return {
        "inserted": inserted,
        "duplicates_in_file": parsed["duplicates"],
        "skipped_existing": skipped_existing,
        "skipped_invalid": parsed["skipped_invalid"],
    }
