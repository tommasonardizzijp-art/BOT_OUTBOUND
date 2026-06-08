"""Shared campaign control helpers for web API and Telegram commands."""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.account import AccountStatus, InstagramAccount
from app.models.activity_log import ActivityLog
from app.models.campaign import Campaign, CampaignStatus
from app.models.campaign_account import CampaignAccount
from app.models.follower import Follower, FollowerStatus
from app.services.work_enqueue import (
    enqueue_bios,
    enqueue_campaign_run,
    enqueue_collection,
    enqueue_list,
)


PAUSABLE_STATUSES = (
    CampaignStatus.running,
    CampaignStatus.listing,
    CampaignStatus.listing_break,
    CampaignStatus.scraping,
    CampaignStatus.scraping_and_running,
    CampaignStatus.scraping_break,
)

RESUMABLE_STATUSES = (
    CampaignStatus.paused,
    CampaignStatus.completed,
)


class CampaignControlError(Exception):
    """Expected campaign-control failure that can be shown to an operator."""


def ensure_campaign_can_send_messages(campaign: Campaign) -> None:
    """Validate the shared preconditions for AI generation and DM workers."""
    if not campaign.messaging_enabled:
        raise CampaignControlError(
            "Messaggistica disattivata per questa campagna. "
            "Attiva 'Invia messaggi' e imposta un template prima di inviare DM."
        )
    if len((campaign.base_message_template or "").strip()) < 10:
        raise CampaignControlError(
            "Template messaggio mancante o troppo corto. "
            "Imposta un messaggio base di almeno 10 caratteri prima di inviare DM."
        )


async def ensure_bot_accepts_work(db: AsyncSession) -> None:
    """Block operator start/resume commands while the global kill-switch is active."""
    from app.services.bot_state_service import is_halted

    if await is_halted(db):
        raise CampaignControlError(
            "Bot in pausa globale. Riattivalo dal controllo operatore prima di "
            "avviare o riprendere campagne."
        )


async def check_redis_reachable() -> bool:
    try:
        import redis.asyncio as aioredis

        r = aioredis.from_url(settings.redis_url, socket_connect_timeout=3)
        await r.ping()
        await r.aclose()
        return True
    except Exception:
        return False


async def list_pausable_campaigns(db: AsyncSession) -> list[Campaign]:
    result = await db.execute(
        select(Campaign)
        .where(Campaign.status.in_(PAUSABLE_STATUSES))
        .order_by(Campaign.updated_at.desc())
    )
    return list(result.scalars().all())


async def list_resumable_campaigns(db: AsyncSession) -> list[Campaign]:
    result = await db.execute(
        select(Campaign)
        .where(Campaign.status.in_(RESUMABLE_STATUSES))
        .order_by(Campaign.updated_at.desc())
    )
    return list(result.scalars().all())


async def has_active_role_account(
    db: AsyncSession,
    campaign_id: str,
    roles: tuple[str, ...],
    statuses: tuple[AccountStatus, ...] = (AccountStatus.active, AccountStatus.warming_up),
) -> bool:
    """True if campaign has an enabled assignment to a usable Instagram account."""
    result = await db.execute(
        select(CampaignAccount.id)
        .join(InstagramAccount, InstagramAccount.id == CampaignAccount.account_id)
        .where(
            CampaignAccount.campaign_id == campaign_id,
            CampaignAccount.is_active == True,
            CampaignAccount.role.in_(roles),
            InstagramAccount.status.in_(statuses),
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def pause_campaign_control(
    db: AsyncSession,
    campaign_id: str,
    *,
    by: str,
    reason: str | None = None,
) -> Campaign:
    campaign = await db.scalar(select(Campaign).where(Campaign.id == campaign_id))
    if campaign is None:
        raise CampaignControlError("Campagna non trovata")
    if campaign.status not in PAUSABLE_STATUSES:
        raise CampaignControlError(f"La campagna non e' attiva: stato={campaign.status.value}")

    campaign.status = CampaignStatus.paused
    campaign.scrape_break_until = None
    campaign.scrape_break_prev_status = None
    campaign.updated_at = datetime.utcnow()
    db.add(
        ActivityLog(
            campaign_id=campaign.id,
            action="campaign_paused",
            details=f'{{"by":"{by}","reason":{_json_string(reason)}}}',
        )
    )
    await db.commit()
    await db.refresh(campaign)
    return campaign


async def resume_campaign_control(
    db: AsyncSession,
    campaign_id: str,
    *,
    by: str,
    enqueue: bool = True,
) -> tuple[Campaign, dict[str, int]]:
    campaign = await db.scalar(select(Campaign).where(Campaign.id == campaign_id))
    if campaign is None:
        raise CampaignControlError("Campagna non trovata")
    if campaign.status not in RESUMABLE_STATUSES:
        raise CampaignControlError(f"La campagna non e' riprendibile: stato={campaign.status.value}")

    await ensure_bot_accepts_work(db)

    if not await check_redis_reachable():
        raise CampaignControlError("Redis non raggiungibile: campagna non riavviata")

    # Pre-validate account assignments BEFORE changing status, otherwise we end
    # up with a campaign in "running" with no enqueued worker (silent failure).
    counts = {"scrape_jobs": 0, "dm_jobs": 0}
    if campaign.scrape_completed_at is None:
        has_scrape_account = await has_active_role_account(
            db, campaign_id, ("scraping", "both"), (AccountStatus.active,)
        )
        if not has_scrape_account:
            raise CampaignControlError(
                "Nessun account attivo con ruolo scraping/both. "
                "Assegna un account scraper prima di riprendere."
            )

        if campaign.source_type != "import":
            # Two-phase scraping: la ripresa non deve mai riavviare lo scraper
            # legacy interleaved. Discrimina la fase: scrape_cursor valorizzato =>
            # Fase Lista interrotta a meta' (riprende la lista); altrimenti, se
            # restano follower pending => Fase Bio; altrimenti riparte la lista.
            if campaign.scrape_cursor:
                campaign.status = CampaignStatus.listing
                action = "list_resumed"
            else:
                pending = await db.scalar(
                    select(func.count(Follower.id)).where(
                        Follower.campaign_id == campaign_id,
                        Follower.status == FollowerStatus.pending,
                    )
                ) or 0
                if pending:
                    campaign.status = CampaignStatus.scraping
                    action = "bios_resumed"
                else:
                    campaign.status = CampaignStatus.listing
                    action = "list_resumed"
        else:
            has_dm_account = await has_active_role_account(db, campaign_id, ("dm", "both"))
            if campaign.auto_generate:
                ensure_campaign_can_send_messages(campaign)
            if campaign.auto_generate and not has_dm_account:
                raise CampaignControlError(
                    "auto_generate attivo ma nessun account DM/both: "
                    "assegna un account DM o disattiva auto_generate."
                )
            if campaign.auto_generate and has_dm_account:
                campaign.status = CampaignStatus.scraping_and_running
                action = "campaign_resumed_parallel"
            else:
                campaign.status = CampaignStatus.scraping
                action = "scrape_resumed"
    else:
        ensure_campaign_can_send_messages(campaign)
        if not await has_active_role_account(db, campaign_id, ("dm", "both")):
            raise CampaignControlError(
                "Nessun account attivo con ruolo DM/both. "
                "Assegna un account DM prima di riprendere."
            )
        campaign.status = CampaignStatus.running
        action = "campaign_resumed"

    campaign.updated_at = datetime.utcnow()
    db.add(
        ActivityLog(
            campaign_id=campaign.id,
            action=action,
            details=f'{{"by":"{by}"}}',
        )
    )
    await db.commit()
    await db.refresh(campaign)

    if enqueue:
        enqueue_error: str | None = None
        try:
            if campaign.status == CampaignStatus.listing:
                await enqueue_list(campaign_id)
                counts["scrape_jobs"] += 1
            elif campaign.status == CampaignStatus.scraping and campaign.source_type != "import":
                # source_type=scrape => Fase Bio (mai lo scraper legacy).
                await enqueue_bios(campaign_id)
                counts["scrape_jobs"] += 1
            elif campaign.status in (CampaignStatus.scraping, CampaignStatus.scraping_and_running):
                # import resolve, oppure parallelo legacy.
                await enqueue_collection(campaign_id, campaign.source_type)
                counts["scrape_jobs"] += 1
            if campaign.status in (CampaignStatus.running, CampaignStatus.scraping_and_running):
                counts["dm_jobs"] += await enqueue_campaign_run(campaign_id)
                if counts["dm_jobs"] == 0:
                    enqueue_error = (
                        "Nessun worker DM accodato (account assegnato rimosso o disattivato "
                        "durante la ripresa). Riprova dopo aver verificato gli account."
                    )
        except Exception as exc:
            enqueue_error = f"Errore enqueue worker: {str(exc)[:180]}"

        if enqueue_error:
            campaign.status = CampaignStatus.paused
            campaign.updated_at = datetime.utcnow()
            db.add(
                ActivityLog(
                    campaign_id=campaign.id,
                    action="campaign_resume_failed",
                    details=f'{{"by":"{by}","error":{_json_string(enqueue_error)}}}',
                )
            )
            await db.commit()
            raise CampaignControlError(enqueue_error)

    return campaign, counts


async def pause_campaigns_without_usable_dm_accounts(db: AsyncSession, account_id: str) -> int:
    """Pause active DM campaigns assigned to account_id if they have no usable DM account left."""
    rows = await db.execute(
        select(Campaign.id)
        .join(CampaignAccount, CampaignAccount.campaign_id == Campaign.id)
        .where(
            CampaignAccount.account_id == account_id,
            CampaignAccount.is_active == True,
            CampaignAccount.role.in_(("dm", "both")),
            Campaign.status.in_((CampaignStatus.running, CampaignStatus.scraping_and_running)),
        )
    )
    campaign_ids = [row[0] for row in rows.all()]
    paused = 0
    for campaign_id in campaign_ids:
        usable = await db.scalar(
            select(CampaignAccount.id)
            .join(InstagramAccount, InstagramAccount.id == CampaignAccount.account_id)
            .where(
                CampaignAccount.campaign_id == campaign_id,
                CampaignAccount.is_active == True,
                CampaignAccount.role.in_(("dm", "both")),
                InstagramAccount.status.in_((AccountStatus.active, AccountStatus.warming_up)),
            )
            .limit(1)
        )
        if usable:
            continue
        result = await db.execute(
            update(Campaign)
            .where(Campaign.id == campaign_id)
            .values(status=CampaignStatus.paused, updated_at=datetime.utcnow())
        )
        paused += result.rowcount or 0
    return paused


def _json_string(value: str | None) -> str:
    return json.dumps(value[:500] if value is not None else None)
