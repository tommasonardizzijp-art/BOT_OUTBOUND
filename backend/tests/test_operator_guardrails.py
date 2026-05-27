import pytest
from fastapi import HTTPException

from app.api.accounts import _raise_if_account_is_in_active_campaigns
from app.api.campaign_accounts import _block_structural_change_while_active
from app.models.campaign import Campaign, CampaignStatus
from app.services.work_enqueue import (
    DM_STARTUP_STAGGER_MAX_SECONDS,
    dm_worker_job_id,
    dm_worker_redis_keys,
    _dm_worker_queued_detail,
)


def _campaign(status: CampaignStatus) -> Campaign:
    return Campaign(
        name="Test",
        target_username="target",
        base_message_template="Messaggio abbastanza lungo",
        status=status,
    )


def test_structural_account_changes_require_pausing_active_campaign():
    with pytest.raises(HTTPException) as exc:
        _block_structural_change_while_active(_campaign(CampaignStatus.running))

    assert exc.value.status_code == 409
    assert "mettila in pausa" in exc.value.detail


def test_structural_account_changes_are_allowed_while_paused():
    _block_structural_change_while_active(_campaign(CampaignStatus.paused))


def test_disabling_account_reports_active_campaigns():
    with pytest.raises(HTTPException) as exc:
        _raise_if_account_is_in_active_campaigns(["Alpha", "Beta"])

    assert exc.value.status_code == 409
    assert '"Alpha"' in exc.value.detail
    assert "disabilitare o eliminare" in exc.value.detail


def test_dm_startup_stagger_is_bounded_to_five_minutes():
    assert DM_STARTUP_STAGGER_MAX_SECONDS == 5 * 60


def test_worker_queued_detail_makes_defer_visible():
    assert "entro 1 min" in _dm_worker_queued_detail("alpha", 12)
    assert "circa 5 min" in _dm_worker_queued_detail("beta", 300)


def test_dm_worker_redis_keys_cover_deferred_and_running_jobs():
    campaign_id = "camp"
    account_id = "acct"
    job_id = dm_worker_job_id(campaign_id, account_id)

    assert job_id == "worker:camp:acct"
    assert dm_worker_redis_keys(campaign_id, account_id) == (
        "arq:job:worker:camp:acct",
        "arq:retry:worker:camp:acct",
        "arq:in-progress:worker:camp:acct",
    )
