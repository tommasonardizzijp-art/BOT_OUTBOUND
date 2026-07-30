"""/reset per campagne IMPORT: il reset deve considerare le righe ImportedProfile
ancora pending/resolving, non solo i Follower gia' risolti (sintomo A, vedi memoria
botoutbound-campagne-import-macchina-stati). Oggi (bug) un import con follower gia'
risolti ma righe pending residue va a 'ready' e resta bloccato per sempre: start-scrape
per import riparte solo da draft/error, quindi la lista non viene mai completata.

Pattern preso da test_campaigns_start_dm_auto.py: chiamata diretta alla funzione
endpoint (non HTTP/ASGITransport) con AsyncSessionLocal, perche' non esiste una
fixture `campaign_factory` ne' un client HTTP condiviso nella suite.
"""
import uuid

import pytest

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.models.imported_profile import ImportedProfile


async def _import_campaign(db, status=CampaignStatus.paused) -> Campaign:
    campaign = Campaign(
        id=str(uuid.uuid4()),
        name="import-reset-test",
        source_type="import",
        target_username="target_user",
        scrape_mode="followers",
        status=status,
        messaging_enabled=False,
    )
    db.add(campaign)
    await db.flush()
    return campaign


@pytest.mark.asyncio
async def test_reset_import_with_pending_rows_goes_to_error_not_ready():
    """Import con follower gia' risolti MA righe pending ancora da lavorare: reset deve
    portare a 'error' (start-scrape la riprende), non a 'ready' (bloccato)."""
    from app.api import campaigns as capi

    async with AsyncSessionLocal() as db:
        campaign = await _import_campaign(db)
        db.add(Follower(
            campaign_id=campaign.id, ig_user_id=1, username="resolved_one",
            status=FollowerStatus.bio_scraped,
        ))
        db.add(ImportedProfile(
            campaign_id=campaign.id, raw_input="resolved_one", username="resolved_one", status="resolved",
        ))
        db.add(ImportedProfile(
            campaign_id=campaign.id, raw_input="still_pending", username="still_pending", status="pending",
        ))
        await db.commit()

        result = await capi.reset_campaign(campaign.id, db)

    assert result.status == "error"


@pytest.mark.asyncio
async def test_reset_import_fully_resolved_goes_to_ready():
    """Import completamente risolto (nessuna riga pending): reset va a 'ready' come oggi."""
    from app.api import campaigns as capi

    async with AsyncSessionLocal() as db:
        campaign = await _import_campaign(db)
        db.add(Follower(
            campaign_id=campaign.id, ig_user_id=1, username="resolved_one",
            status=FollowerStatus.bio_scraped,
        ))
        db.add(ImportedProfile(
            campaign_id=campaign.id, raw_input="resolved_one", username="resolved_one", status="resolved",
        ))
        await db.commit()

        result = await capi.reset_campaign(campaign.id, db)

    assert result.status == "ready"


@pytest.mark.asyncio
async def test_reset_import_no_followers_no_pending_goes_to_draft():
    """Import senza alcun follower risolto e senza righe pending (es. tutte not_found/error):
    reset va a draft E rimette a pending le righe not_found/error per poterle ritentare."""
    from app.api import campaigns as capi

    async with AsyncSessionLocal() as db:
        campaign = await _import_campaign(db)
        db.add(ImportedProfile(
            campaign_id=campaign.id, raw_input="bad_one", username="bad_one", status="not_found",
        ))
        await db.commit()

        result = await capi.reset_campaign(campaign.id, db)

    assert result.status == "draft"
