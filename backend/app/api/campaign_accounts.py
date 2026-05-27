"""
Campaign ↔ Account assignment management.

Endpoints let you assign one or more Instagram accounts to a campaign,
set per-account-per-campaign daily limits, enable/disable individual
account assignments, and remove them.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models.campaign import Campaign, CampaignStatus
from app.models.account import InstagramAccount
from app.models.campaign_account import CampaignAccount
from app.schemas.campaign_account import (
    CampaignAccountAssign,
    CampaignAccountUpdate,
    CampaignAccountResponse,
)

router = APIRouter(prefix="/campaigns/{campaign_id}/accounts", tags=["campaign-accounts"])

ACTIVE_CAMPAIGN_STATUSES = (
    CampaignStatus.running,
    CampaignStatus.scraping,
    CampaignStatus.scraping_and_running,
    CampaignStatus.scraping_break,
)


async def _get_campaign_or_404(campaign_id: str, db: AsyncSession) -> Campaign:
    result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


def _block_structural_change_while_active(campaign: Campaign) -> None:
    if campaign.status in ACTIVE_CAMPAIGN_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                "Campagna attiva: mettila in pausa prima di aggiungere, rimuovere, "
                "abilitare/disabilitare o cambiare ruolo agli account assegnati."
            ),
        )


def _build_response(ca: CampaignAccount, username: str) -> CampaignAccountResponse:
    return CampaignAccountResponse(
        id=ca.id,
        campaign_id=ca.campaign_id,
        account_id=ca.account_id,
        account_username=username,
        daily_limit_override=ca.daily_limit_override,
        is_active=ca.is_active,
        role=getattr(ca, 'role', 'both'),
        created_at=ca.created_at,
    )


@router.get("", response_model=list[CampaignAccountResponse])
async def list_campaign_accounts(
    campaign_id: str,
    db: AsyncSession = Depends(get_db),
):
    """List all account assignments for a campaign (active and inactive)."""
    await _get_campaign_or_404(campaign_id, db)

    result = await db.execute(
        select(CampaignAccount, InstagramAccount.username)
        .join(InstagramAccount, CampaignAccount.account_id == InstagramAccount.id)
        .where(CampaignAccount.campaign_id == campaign_id)
        .order_by(CampaignAccount.created_at)
    )
    return [_build_response(ca, username) for ca, username in result.all()]


@router.post("", response_model=CampaignAccountResponse, status_code=201)
async def assign_account(
    campaign_id: str,
    data: CampaignAccountAssign,
    force: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
):
    """Assign an Instagram account to this campaign."""
    campaign = await _get_campaign_or_404(campaign_id, db)
    _block_structural_change_while_active(campaign)

    # Validate account exists
    acc_result = await db.execute(
        select(InstagramAccount).where(InstagramAccount.id == data.account_id)
    )
    account = acc_result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    # Check not already assigned to THIS campaign
    existing = await db.execute(
        select(CampaignAccount).where(
            CampaignAccount.campaign_id == campaign_id,
            CampaignAccount.account_id == data.account_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Account already assigned to this campaign")

    # 7B Lite: warn if account is active in another running/paused campaign
    if not force:
        from app.models.campaign import Campaign as CampaignModel, CampaignStatus
        conflict_result = await db.execute(
            select(CampaignModel.name)
            .join(CampaignAccount, CampaignModel.id == CampaignAccount.campaign_id)
            .where(
                CampaignAccount.account_id == data.account_id,
                CampaignAccount.campaign_id != campaign_id,
                CampaignAccount.is_active == True,
                CampaignModel.status.in_([
                    CampaignStatus.running, CampaignStatus.paused,
                    CampaignStatus.scraping_and_running, CampaignStatus.scraping_break,
                ]),
            )
        )
        conflict_names = [row[0] for row in conflict_result.all()]
        if conflict_names:
            names = ", ".join(f'"{n}"' for n in conflict_names)
            raise HTTPException(
                status_code=409,
                detail=f"ACCOUNT_IN_USE:{names}",
            )

    ca = CampaignAccount(
        campaign_id=campaign_id,
        account_id=data.account_id,
        daily_limit_override=data.daily_limit_override,
        role=data.role,
    )
    db.add(ca)
    await db.commit()
    await db.refresh(ca)

    return _build_response(ca, account.username)


@router.put("/{account_id}", response_model=CampaignAccountResponse)
async def update_campaign_account(
    campaign_id: str,
    account_id: str,
    data: CampaignAccountUpdate,
    db: AsyncSession = Depends(get_db),
):
    """
    Update per-account-per-campaign settings.
    Set `daily_limit_override` to null to revert to the account's global limit.
    """
    campaign = await _get_campaign_or_404(campaign_id, db)
    if data.is_active is not None or data.role is not None:
        _block_structural_change_while_active(campaign)

    result = await db.execute(
        select(CampaignAccount, InstagramAccount.username)
        .join(InstagramAccount, CampaignAccount.account_id == InstagramAccount.id)
        .where(
            CampaignAccount.campaign_id == campaign_id,
            CampaignAccount.account_id == account_id,
        )
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Account assignment not found")

    ca, username = row

    # Allow explicit null to clear the override
    if "daily_limit_override" in data.model_fields_set or data.daily_limit_override is not None:
        ca.daily_limit_override = data.daily_limit_override
    if data.is_active is not None:
        ca.is_active = data.is_active
    if data.role is not None:
        ca.role = data.role

    await db.commit()
    await db.refresh(ca)
    return _build_response(ca, username)


@router.delete("/{account_id}", status_code=204)
async def unassign_account(
    campaign_id: str,
    account_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Remove an account from this campaign."""
    campaign = await _get_campaign_or_404(campaign_id, db)
    _block_structural_change_while_active(campaign)

    result = await db.execute(
        select(CampaignAccount).where(
            CampaignAccount.campaign_id == campaign_id,
            CampaignAccount.account_id == account_id,
        )
    )
    ca = result.scalar_one_or_none()
    if not ca:
        raise HTTPException(status_code=404, detail="Account assignment not found")

    await db.delete(ca)
    await db.commit()
