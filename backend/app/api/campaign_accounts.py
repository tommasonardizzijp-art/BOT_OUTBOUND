"""
Campaign ↔ Account assignment management.

Endpoints let you assign one or more Instagram accounts to a campaign,
set per-account-per-campaign daily limits, enable/disable individual
account assignments, and remove them.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.database import get_db
from app.models.campaign import Campaign, CampaignStatus
from app.models.account import InstagramAccount
from app.models.campaign_account import CampaignAccount
from app.schemas.campaign_account import (
    CampaignAccountAssign,
    CampaignAccountUpdate,
    CampaignAccountResponse,
)
from app.utils.roles import INBOX_ROLES, is_inbox

router = APIRouter(prefix="/campaigns/{campaign_id}/accounts", tags=["campaign-accounts"])

ACTIVE_CAMPAIGN_STATUSES = (
    CampaignStatus.running,
    CampaignStatus.listing,
    CampaignStatus.listing_break,
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


async def _ensure_no_other_inbox_account(
    db: AsyncSession, campaign_id: str, exclude_account_id: str | None
) -> None:
    """Raise 400 if another account on this campaign already carries the inbox
    capability. Counts ALL rows (active + inactive) so a disabled inbox account
    can't be bypassed by adding/promoting a second one. `exclude_account_id`
    skips the row being updated in place (role change on the same account)."""
    query = (
        select(func.count(CampaignAccount.account_id))
        .where(
            CampaignAccount.campaign_id == campaign_id,
            CampaignAccount.role.in_(INBOX_ROLES),
        )
    )
    if exclude_account_id is not None:
        query = query.where(CampaignAccount.account_id != exclude_account_id)
    existing_inbox = await db.scalar(query) or 0
    if existing_inbox >= 1:
        raise HTTPException(
            status_code=400,
            detail="Una campagna può avere un solo account con capability inbox (lettura DM).",
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
                    CampaignStatus.listing, CampaignStatus.listing_break,
                    CampaignStatus.scraping,
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

    # Cap: una sola capability inbox per campagna (un account legge una sola
    # inbox DM). Gli account scraping/dm/both restano illimitati. Conta TUTTE
    # le righe inbox (anche is_active=False) per evitare il bypass disattiva+aggiungi.
    if is_inbox(data.role):
        await _ensure_no_other_inbox_account(db, campaign_id, exclude_account_id=None)

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
        # Promoting an account to an inbox-capable role: enforce the 1-inbox cap
        # (excluding this same row, which may already be inbox-capable).
        if is_inbox(data.role) and not is_inbox(ca.role):
            await _ensure_no_other_inbox_account(db, campaign_id, exclude_account_id=account_id)
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
