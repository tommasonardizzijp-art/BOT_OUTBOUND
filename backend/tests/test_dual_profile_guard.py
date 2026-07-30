import uuid

import pytest

from app.services.campaign_control import has_dedicated_scrape_and_dm_accounts
from app.models.campaign import Campaign
from app.models.campaign_account import CampaignAccount
from app.models.account import InstagramAccount, AccountStatus


def _make_campaign(db, name: str = "guard-test") -> Campaign:
    campaign = Campaign(
        id=str(uuid.uuid4()),
        name=name,
        source_type="scrape",
        target_username="target_user",
        scrape_mode="followers",
        messaging_enabled=False,
    )
    db.add(campaign)
    return campaign


async def _make_account(db, role, status=AccountStatus.active):
    acc = InstagramAccount(
        username=f"acc_{role}_{uuid.uuid4().hex[:8]}",
        status=status,
        session_data="{}",
        encrypted_password="x",
    )
    db.add(acc)
    await db.flush()
    return acc


async def _assign(db, campaign_id, account, role, is_active=True):
    ca = CampaignAccount(campaign_id=campaign_id, account_id=account.id, role=role, is_active=is_active)
    db.add(ca)
    await db.flush()
    return ca


@pytest.mark.asyncio
async def test_no_accounts_returns_false(db_session):
    campaign = _make_campaign(db_session)
    await db_session.flush()
    assert await has_dedicated_scrape_and_dm_accounts(db_session, campaign.id) is False


@pytest.mark.asyncio
async def test_single_both_account_returns_false(db_session):
    """Un solo profilo role='both' NON basta: non e' dedicato ne' a scrape ne' a dm da solo."""
    campaign = _make_campaign(db_session)
    await db_session.flush()
    acc = await _make_account(db_session, "both")
    await _assign(db_session, campaign.id, acc, "both")
    await db_session.commit()
    assert await has_dedicated_scrape_and_dm_accounts(db_session, campaign.id) is False


@pytest.mark.asyncio
async def test_two_both_accounts_returns_false(db_session):
    """Due profili 'both' non contano come dedicati: nessuno dei due e' scraping-only o dm-only."""
    campaign = _make_campaign(db_session)
    await db_session.flush()
    acc1 = await _make_account(db_session, "both")
    acc2 = await _make_account(db_session, "both")
    await _assign(db_session, campaign.id, acc1, "both")
    await _assign(db_session, campaign.id, acc2, "both")
    await db_session.commit()
    assert await has_dedicated_scrape_and_dm_accounts(db_session, campaign.id) is False


@pytest.mark.asyncio
async def test_one_scraping_only_one_dm_only_returns_true(db_session):
    campaign = _make_campaign(db_session)
    await db_session.flush()
    acc1 = await _make_account(db_session, "scraping")
    acc2 = await _make_account(db_session, "dm")
    await _assign(db_session, campaign.id, acc1, "scraping")
    await _assign(db_session, campaign.id, acc2, "dm")
    await db_session.commit()
    assert await has_dedicated_scrape_and_dm_accounts(db_session, campaign.id) is True


@pytest.mark.asyncio
async def test_scraping_only_present_but_dm_only_inactive_returns_false(db_session):
    campaign = _make_campaign(db_session)
    await db_session.flush()
    acc1 = await _make_account(db_session, "scraping")
    acc2 = await _make_account(db_session, "dm")
    await _assign(db_session, campaign.id, acc1, "scraping")
    await _assign(db_session, campaign.id, acc2, "dm", is_active=False)
    await db_session.commit()
    assert await has_dedicated_scrape_and_dm_accounts(db_session, campaign.id) is False


@pytest.mark.asyncio
async def test_inbox_scraping_and_inbox_dm_count_as_dedicated(db_session):
    """inbox_scraping/inbox_dm sono comunque a singola capability (scrape XOR dm), inbox e' ortogonale."""
    campaign = _make_campaign(db_session)
    await db_session.flush()
    acc1 = await _make_account(db_session, "inbox_scraping")
    acc2 = await _make_account(db_session, "inbox_dm")
    await _assign(db_session, campaign.id, acc1, "inbox_scraping")
    await _assign(db_session, campaign.id, acc2, "inbox_dm")
    await db_session.commit()
    assert await has_dedicated_scrape_and_dm_accounts(db_session, campaign.id) is True
