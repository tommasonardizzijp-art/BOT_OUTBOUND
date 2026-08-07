"""resume_campaign_control per campagne IMPORT in parallelo (auto_generate + DM):
stessa guard 2-profili-dedicati di has_dedicated_scrape_and_dm_accounts (Task 1),
gia' applicata a start-dm-auto (Task 2). Un solo account 'both' non basta: farebbe
scraping e DM in parallelo sullo stesso profilo, il pattern che genera checkpoint IG.

Pattern preso da test_campaigns_start_dm_auto.py: nessuna fixture `campaign_factory`
nella suite, si costruisce Campaign/InstagramAccount/CampaignAccount inline via
AsyncSessionLocal, e si chiama resume_campaign_control direttamente (non via HTTP).
"""
import uuid

import pytest

from app.database import AsyncSessionLocal
from app.models.account import AccountStatus, InstagramAccount
from app.models.campaign import Campaign, CampaignStatus
from app.models.campaign_account import CampaignAccount
from app.services.campaign_control import CampaignControlError, resume_campaign_control


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
        name="import-resume-test",
        source_type="import",
        target_username="target_user",
        scrape_mode="followers",
        status=CampaignStatus.paused,
        auto_generate=True,
        messaging_enabled=True,
        base_message_template="ciao " * 5,
        scrape_completed_at=None,
    )
    db.add(campaign)
    await db.flush()
    return campaign


@pytest.mark.asyncio
async def test_resume_parallel_import_blocked_without_dedicated_accounts(monkeypatch):
    async def _ok(*a, **kw):
        return True

    async def _no_halt(*a, **kw):
        return None

    monkeypatch.setattr("app.services.campaign_control.check_redis_reachable", _ok)
    monkeypatch.setattr("app.services.campaign_control.ensure_bot_accepts_work", _no_halt)

    async with AsyncSessionLocal() as db:
        campaign = await _import_campaign(db)
        acc = await _account(db, "both")
        db.add(CampaignAccount(campaign_id=campaign.id, account_id=acc.id, role="both", is_active=True))
        await db.commit()

        with pytest.raises(CampaignControlError, match="dedicat"):
            await resume_campaign_control(db, campaign.id, by="test", enqueue=False)


@pytest.mark.asyncio
async def test_resume_parallel_import_allowed_with_two_dedicated_accounts(monkeypatch):
    async def _ok(*a, **kw):
        return True

    async def _no_halt(*a, **kw):
        return None

    async def _fake_enqueue(campaign_id):
        return 1

    monkeypatch.setattr("app.services.campaign_control.check_redis_reachable", _ok)
    monkeypatch.setattr("app.services.campaign_control.ensure_bot_accepts_work", _no_halt)
    monkeypatch.setattr("app.services.work_enqueue.enqueue_campaign_run", _fake_enqueue)

    async with AsyncSessionLocal() as db:
        campaign = await _import_campaign(db)
        acc_scrape = await _account(db, "scraping")
        acc_dm = await _account(db, "dm")
        db.add(CampaignAccount(campaign_id=campaign.id, account_id=acc_scrape.id, role="scraping", is_active=True))
        db.add(CampaignAccount(campaign_id=campaign.id, account_id=acc_dm.id, role="dm", is_active=True))
        await db.commit()

        updated_campaign, counts = await resume_campaign_control(db, campaign.id, by="test", enqueue=False)

        from sqlalchemy import select as _select

        from app.models.activity_log import ActivityLog

        log = await db.scalar(
            _select(ActivityLog)
            .where(ActivityLog.campaign_id == campaign.id)
            .order_by(ActivityLog.created_at.desc())
        )

    assert updated_campaign.status == CampaignStatus.scraping_and_running
    assert log is not None
    assert log.action == "campaign_resumed_parallel"
