import pytest
from fastapi import HTTPException

from app.api.accounts import _raise_if_account_is_in_active_campaigns
from app.api.campaign_accounts import _block_structural_change_while_active
from app.models.campaign import Campaign, CampaignStatus
from app.services.work_enqueue import (
    DM_STARTUP_STAGGER_MAX_SECONDS,
    DM_STARTUP_STAGGER_MIN_SECONDS,
    campaign_cleanup_redis_keys,
    dm_startup_stagger_seconds,
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
    assert DM_STARTUP_STAGGER_MIN_SECONDS == 3 * 60
    assert DM_STARTUP_STAGGER_MAX_SECONDS == 5 * 60


def test_first_dm_worker_starts_immediately():
    assert dm_startup_stagger_seconds(0) == 0


def test_later_dm_workers_are_shifted_three_to_five_minutes_each():
    for index in (1, 2, 3):
        for _ in range(20):
            defer = dm_startup_stagger_seconds(index)
            assert index * DM_STARTUP_STAGGER_MIN_SECONDS <= defer <= index * DM_STARTUP_STAGGER_MAX_SECONDS


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


def test_campaign_cleanup_keys_cover_every_phase_jobid_scheme():
    """Regressione: prima list:/bios:/biobrowser:/importbrowser: non venivano
    purgati alla delete -> job zombie che sparavano `Campaign ... not found`."""
    keys = set(campaign_cleanup_redis_keys("camp", ["a1", "a2"]))

    # Fasi con fan-out per-account (incluse quelle che prima erano orfane).
    for acct in ("a1", "a2"):
        for prefix in ("worker", "biobrowser", "importbrowser"):
            assert f"arq:job:{prefix}:camp:{acct}" in keys
            assert f"arq:retry:{prefix}:camp:{acct}" in keys
            assert f"arq:in-progress:{prefix}:camp:{acct}" in keys

    # Fasi mono-job: list e bios erano quelle che perdevano job in defer.
    for suffix in (
        "list:camp",
        "bios:camp",
        "scrape:camp",
        "resolve:camp",
        "pregen:camp:preview",
        "pregen:camp:full",
    ):
        assert f"arq:job:{suffix}" in keys
        assert f"arq:retry:{suffix}" in keys
        assert f"arq:in-progress:{suffix}" in keys


def test_campaign_cleanup_keys_have_no_duplicates():
    keys = campaign_cleanup_redis_keys("camp", ["a1"])
    assert len(keys) == len(set(keys))


def test_campaign_cleanup_keys_without_accounts_still_purge_phase_jobs():
    """Campagna import senza CampaignAccount: le chiavi mono-job vanno purgate lo stesso."""
    keys = campaign_cleanup_redis_keys("camp", [])
    assert "arq:job:list:camp" in keys
    assert "arq:job:bios:camp" in keys
