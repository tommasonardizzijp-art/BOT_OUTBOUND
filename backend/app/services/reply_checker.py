"""
Reply checker: scans Instagram DM inbox for replies from followers we contacted.
Uses instagrapi (same client as scraper) to check the DM inbox of each account.

Called by the ARQ cron every 30 minutes. Sets Follower.status = 'replied' when
a reply is detected, which the Leads page picks up via the JOIN on ig_user_id.
"""
import asyncio
import json
from datetime import datetime
from loguru import logger
from sqlalchemy import select, func

from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount, AccountStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.campaign_account import CampaignAccount
from app.models.follower import Follower, FollowerStatus
from app.models.activity_log import ActivityLog
from app.utils.instagrapi_client import login as _login


async def check_all_replies() -> int:
    """
    Check all campaigns that have sent followers for DM replies.
    Returns total number of newly detected replies.
    Called by the ARQ cron every 30 minutes.
    """
    async with AsyncSessionLocal() as db:
        # Check campaigns that have sent at least some DMs
        campaigns_result = await db.execute(
            select(Campaign).where(
                Campaign.status.in_([
                    CampaignStatus.running,
                    CampaignStatus.scraping_and_running,
                    CampaignStatus.paused,
                    CampaignStatus.completed,
                ])
            )
        )
        campaigns = campaigns_result.scalars().all()

        total_replied = 0
        for campaign in campaigns:
            campaign_id = campaign.id
            campaign_name = campaign.name  # capture before try — avoids lazy-load on broken session
            try:
                count = await _check_campaign(campaign_id, db)
                total_replied += count
            except Exception as e:
                logger.warning(f"[ReplyChecker] Campaign '{campaign_name}' check failed: {e}")

        return total_replied


async def _check_campaign(campaign_id: str, db) -> int:
    """Check one campaign for replies. Returns count of new replies detected."""
    from app.models.message import Message

    # Followers with status=sent — candidates for reply detection.
    # Keep last sent_at so old conversations do not inflate reply metrics.
    sent_result = await db.execute(
        select(Follower, func.max(Message.sent_at))
        .join(Message, Message.follower_id == Follower.id, isouter=True)
        .where(
            Follower.campaign_id == campaign_id,
            Follower.status == FollowerStatus.sent,
        )
        .group_by(Follower.id)
    )
    sent_followers = {}
    for follower, last_sent in sent_result.all():
        sent_followers[follower.ig_user_id] = (follower, last_sent)

    if not sent_followers:
        return 0

    # Accounts assigned to this campaign
    ca_result = await db.execute(
        select(CampaignAccount).where(
            CampaignAccount.campaign_id == campaign_id,
            CampaignAccount.is_active == True,
            CampaignAccount.role.in_(("dm", "both")),
        )
    )
    campaign_accounts = ca_result.scalars().all()

    replied_count = 0
    for ca in campaign_accounts:
        acc_result = await db.execute(
            select(InstagramAccount).where(InstagramAccount.id == ca.account_id)
        )
        account = acc_result.scalar_one_or_none()
        if not account or account.status in (AccountStatus.banned, AccountStatus.disabled, AccountStatus.challenge_required):
            continue

        account_username = account.username  # capture before try — avoids lazy-load on broken session
        try:
            count = await _scan_inbox(account, sent_followers, db)
            replied_count += count
        except Exception as e:
            logger.warning(f"[ReplyChecker] Inbox scan failed for @{account_username}: {e}")

    if replied_count > 0:
        await db.commit()

    return replied_count


def _message_is_after_send(msg, last_sent: datetime | None) -> bool:
    if last_sent is None:
        return True

    ts = getattr(msg, "timestamp", None)
    if ts is None:
        return False

    if isinstance(ts, datetime):
        msg_at = ts.replace(tzinfo=None)
    elif isinstance(ts, (int, float)):
        # Some client versions expose Unix seconds/ms instead of datetime.
        seconds = ts / 1000 if ts > 10_000_000_000 else ts
        msg_at = datetime.utcfromtimestamp(seconds)
    else:
        return False

    return msg_at > last_sent.replace(tzinfo=None)


async def _scan_inbox(account: InstagramAccount, sent_followers: dict, db) -> int:
    """
    Login as `account` and scan DM inbox for replies from `sent_followers`.
    A reply is detected when a thread contains a message NOT sent by us.
    Returns count of newly marked replied followers.
    """
    from pydantic import ValidationError as PydanticValidationError

    # skip_gql_verify: the GQL ping is only needed before mobile scraping to avoid
    # UFAC challenge — it fails with 400 on some sessions and is not needed for
    # reading the DM inbox, which uses a different API surface.
    client = await _login(account, db, skip_gql_verify=True)
    own_pk = int(client.user_id)

    # Fetch recent inbox threads — direct_threads() returns list[DirectThread]
    # Guard against ValidationError: IG sometimes returns media objects (e.g.
    # MediaXma) with null fields that pydantic rejects. Treat as inconclusive
    # rather than crashing the entire reply-check run for this account.
    try:
        threads = await asyncio.to_thread(client.direct_threads, amount=200)
    except PydanticValidationError as exc:
        logger.warning(
            f"[ReplyChecker] @{account.username}: direct_threads parse error "
            f"(MediaXma or similar) — skipping inbox scan for this account. "
            f"Error: {str(exc)[:200]}"
        )
        return 0
    try:
        pending = await asyncio.to_thread(client.direct_pending_inbox, 100)
        threads = list(threads) + list(pending)
    except PydanticValidationError as exc:
        logger.debug(f"[ReplyChecker] pending inbox parse error, using main inbox only: {exc}")
    except Exception as e:
        logger.debug(f"[ReplyChecker] pending inbox non disponibile: {e}")

    replied_count = 0
    marked_users: set[int] = set()
    for thread in threads:
        other_users = []
        for user in thread.users:
            try:
                user_pk = int(user.pk)
            except Exception:
                continue
            if user_pk != own_pk:
                other_users.append((user, user_pk))

        # Skip groups: cold outreach reply metrics are 1:1 only.
        if len(other_users) != 1:
            continue

        user, user_pk = other_users[0]
        if user_pk in marked_users or user_pk not in sent_followers:
            continue

        follower, last_sent = sent_followers[user_pk]

        has_reply = any(
            hasattr(msg, "user_id")
            and msg.user_id
            and int(msg.user_id) == user_pk
            and _message_is_after_send(msg, last_sent)
            for msg in thread.messages
        )

        if has_reply:
            follower.status = FollowerStatus.replied
            follower.updated_at = datetime.utcnow()

            log = ActivityLog(
                campaign_id=follower.campaign_id,
                action="reply_detected",
                details=json.dumps({
                    "username": follower.username,
                    "ig_user_id": user_pk,
                    "account": account.username,
                }),
            )
            db.add(log)

            logger.info(
                f"[ReplyChecker] Reply from @{follower.username} detected via @{account.username}"
            )
            marked_users.add(user_pk)
            replied_count += 1

    return replied_count
