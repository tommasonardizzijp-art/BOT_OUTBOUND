from app.models.account import AccountStatus, InstagramAccount
from app.models.campaign import Campaign, CampaignStatus
from app.models.campaign_account import CampaignAccount
from app.services.recovery_checker import _can_resume_dm_worker


def _campaign(status: CampaignStatus) -> Campaign:
    return Campaign(
        id="campaign",
        name="Recovery",
        target_username="target",
        base_message_template="Ciao",
        status=status,
    )


def _account(status: AccountStatus) -> InstagramAccount:
    return InstagramAccount(
        id="account",
        username="sender",
        encrypted_password="secret",
        status=status,
    )


def _assignment(*, is_active: bool = True, role: str = "dm") -> CampaignAccount:
    return CampaignAccount(
        campaign_id="campaign",
        account_id="account",
        is_active=is_active,
        role=role,
    )


def test_recovery_can_resume_running_dm_assignment():
    assert _can_resume_dm_worker(
        _campaign(CampaignStatus.running),
        _account(AccountStatus.active),
        _assignment(),
    )


def test_recovery_does_not_resume_paused_or_non_dm_assignment():
    assert not _can_resume_dm_worker(
        _campaign(CampaignStatus.paused),
        _account(AccountStatus.active),
        _assignment(),
    )
    assert not _can_resume_dm_worker(
        _campaign(CampaignStatus.running),
        _account(AccountStatus.active),
        _assignment(role="scraping"),
    )
