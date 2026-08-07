"""start-dm-auto per campagne IMPORT: sbloccato (scraping+DM in parallelo come le scrape),
ma guardato dalla stessa regola dei 2-profili-dedicati di has_dedicated_scrape_and_dm_accounts
(Task 1). Un solo account 'both' non basta piu' di quanto bastasse prima: cambia solo il
messaggio (ora menziona i profili dedicati, non piu' "DM in parallelo non disponibile
per import").

Pattern preso da test_import_browser_e2e.py: chiamata diretta alla funzione endpoint
(non HTTP/ASGITransport) con AsyncSessionLocal + monkeypatch dei boundary Redis/enqueue,
perche' non esiste una fixture `campaign_factory` ne' un client HTTP condiviso nella suite.
"""
import uuid

import pytest
from fastapi import HTTPException

from app.database import AsyncSessionLocal
from app.models.account import AccountStatus, InstagramAccount
from app.models.campaign import Campaign, CampaignStatus
from app.models.campaign_account import CampaignAccount


async def _account(db, role: str) -> InstagramAccount:
    acc = InstagramAccount(
        username=f"acc_{role}_{uuid.uuid4().hex[:8]}",
        status=AccountStatus.active,
        session_data="{}",
        encrypted_password="x",
    )
    db.add(acc)
    await db.flush()
    return acc


async def _import_campaign(db) -> Campaign:
    campaign = Campaign(
        id=str(uuid.uuid4()),
        name="import-dm-auto-test",
        source_type="import",
        target_username="target_user",
        scrape_mode="followers",
        status=CampaignStatus.scraping,
        messaging_enabled=True,
        base_message_template="ciao " * 5,
    )
    db.add(campaign)
    await db.flush()
    return campaign


@pytest.mark.asyncio
async def test_start_dm_auto_import_blocked_with_single_both_account(monkeypatch):
    """Import in scraping, un solo account 'both': resta bloccato (nessun profilo dedicato)."""
    from app.api import campaigns as capi

    async with AsyncSessionLocal() as db:
        campaign = await _import_campaign(db)
        acc = await _account(db, "both")
        db.add(CampaignAccount(campaign_id=campaign.id, account_id=acc.id, role="both", is_active=True))
        await db.commit()

        with pytest.raises(HTTPException) as exc_info:
            await capi.start_dm_auto(campaign.id, db)

    assert exc_info.value.status_code == 400
    assert "dedicat" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_start_dm_auto_import_allowed_with_two_dedicated_accounts(monkeypatch):
    """Import in scraping, un account scraping-only + un account dm-only: passa a scraping_and_running."""
    from app.api import campaigns as capi

    async def _redis_ok():
        return True

    async def _fake_enqueue(campaign_id):
        return 1

    monkeypatch.setattr(capi, "_check_redis_reachable", _redis_ok)
    monkeypatch.setattr("app.services.work_enqueue.enqueue_campaign_run", _fake_enqueue)

    async with AsyncSessionLocal() as db:
        campaign = await _import_campaign(db)
        acc_scrape = await _account(db, "scraping")
        acc_dm = await _account(db, "dm")
        db.add(CampaignAccount(campaign_id=campaign.id, account_id=acc_scrape.id, role="scraping", is_active=True))
        db.add(CampaignAccount(campaign_id=campaign.id, account_id=acc_dm.id, role="dm", is_active=True))
        await db.commit()

        result = await capi.start_dm_auto(campaign.id, db)

    assert result.status == "scraping_and_running"
