"""/import-retry-failed: requeue bulk delle righe ImportedProfile fallite (not_found/error)
per campagne IMPORT (sintomo B, vedi memoria botoutbound-campagne-import-macchina-stati).
'private' resta escluso: e' un esito definitivo, non un fallimento tecnico da ritentare.

Pattern preso da test_campaigns_import_reset.py: chiamata diretta alla funzione endpoint
(non HTTP/ASGITransport) con AsyncSessionLocal, perche' non esiste una fixture
`campaign_factory` ne' un client HTTP condiviso nella suite.
"""
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.models.imported_profile import ImportedProfile


async def _campaign(db, source_type="import", status=CampaignStatus.ready) -> Campaign:
    campaign = Campaign(
        id=str(uuid.uuid4()),
        name="import-retry-failed-test",
        source_type=source_type,
        target_username="target_user",
        scrape_mode="followers",
        status=status,
        messaging_enabled=False,
    )
    db.add(campaign)
    await db.flush()
    return campaign


@pytest.mark.asyncio
async def test_retry_failed_requeues_not_found_and_error_rows():
    from app.api import campaigns as capi

    async with AsyncSessionLocal() as db:
        campaign = await _campaign(db)
        db.add(Follower(
            campaign_id=campaign.id, ig_user_id=1, username="ok_one",
            status=FollowerStatus.bio_scraped,
        ))
        db.add(ImportedProfile(
            campaign_id=campaign.id, raw_input="ok_one", username="ok_one", status="resolved",
        ))
        db.add(ImportedProfile(
            campaign_id=campaign.id, raw_input="bad_a", username="bad_a", status="not_found",
        ))
        db.add(ImportedProfile(
            campaign_id=campaign.id, raw_input="bad_b", username="bad_b", status="error", error="timeout",
        ))
        db.add(ImportedProfile(
            campaign_id=campaign.id, raw_input="private_one", username="private_one", status="private",
        ))
        await db.commit()

        result = await capi.import_retry_failed(campaign.id, db)

        assert result.status == "error"  # ora ci sono 2 righe pending -> riavviabile

        rows = await db.execute(
            select(ImportedProfile.username, ImportedProfile.status)
            .where(ImportedProfile.campaign_id == campaign.id)
        )
        by_username = {u: s for u, s in rows.all()}
        assert by_username["bad_a"] == "pending"
        assert by_username["bad_b"] == "pending"
        assert by_username["private_one"] == "private"
        assert by_username["ok_one"] == "resolved"


@pytest.mark.asyncio
async def test_retry_failed_on_non_import_campaign_returns_400():
    from app.api import campaigns as capi

    async with AsyncSessionLocal() as db:
        campaign = await _campaign(db, source_type="scrape")
        await db.commit()

        with pytest.raises(HTTPException) as exc_info:
            await capi.import_retry_failed(campaign.id, db)

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_retry_failed_with_nothing_to_retry_returns_400():
    from app.api import campaigns as capi

    async with AsyncSessionLocal() as db:
        campaign = await _campaign(db)
        db.add(ImportedProfile(
            campaign_id=campaign.id, raw_input="ok_one", username="ok_one", status="resolved",
        ))
        await db.commit()

        with pytest.raises(HTTPException) as exc_info:
            await capi.import_retry_failed(campaign.id, db)

    assert exc_info.value.status_code == 400
