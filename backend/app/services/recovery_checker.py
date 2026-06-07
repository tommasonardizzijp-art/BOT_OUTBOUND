"""
Recovery checker: reconciles Message rows stuck in status='sending'.

A row is left in 'sending' when send_dm() raises an exception AFTER pressing
Enter (DM delivered to Instagram) but BEFORE the DB commit that sets status='sent'.

This service is called by:
 - ARQ cron every 5 minutes (task_queue.py).
 - Manual POST /messages/recover-sending endpoint.

For each stale 'sending' row (updated_at older than 10 minutes):
  1. Login as the associated account via instagrapi (session-restore).
  2. Fetch the DM thread with the target user.
  3. Search for an outgoing message whose text matches Message.generated_text.
  4. Match found   -> status=sent, Follower.status=sent, log dm_recovered.
  5. Parse error   -> leave as 'sending' (inconclusive), no anomaly spam.
  6. No match      -> if retry_count < 1: status=retry, retry_count += 1,
                      log dm_recovery_no_evidence.
                      if retry_count >= 1: leave as 'sending' for human inspection.
"""
import asyncio
import json
import re
from datetime import datetime, timedelta
from loguru import logger

from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount, AccountStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.campaign_account import CampaignAccount
from app.models.follower import Follower, FollowerStatus
from app.models.message import Message, MessageStatus
from app.models.activity_log import ActivityLog
from app.utils.instagrapi_client import login as _login
from app.utils.events import emit as emit_event


class _InstagrapiParseError(Exception):
    """Raised when instagrapi fails to parse IG response (e.g. MediaXma.video_url=None).

    Distinct from auth/network errors: delivery status is UNKNOWN, not failed.
    Callers should leave the message as 'sending' and suppress anomaly spam.
    """


# Dedup table: { (account_id, kind) -> last_reported_at }
# Prevents Telegram/notification spam when the same structural error fires for
# every stale 'sending' message on the same account.
_anomaly_last_reported: dict[tuple[str, str], datetime] = {}
_ANOMALY_DEDUP_SECONDS = 1800  # 30 minutes


def _should_report_anomaly(account_id: str | None, kind: str) -> bool:
    """Return True if enough time has passed since the last report of this kind/account."""
    key = (account_id or "", kind)
    last = _anomaly_last_reported.get(key)
    now = datetime.utcnow()
    if last and (now - last).total_seconds() < _ANOMALY_DEDUP_SECONDS:
        return False
    _anomaly_last_reported[key] = now
    return True


def _normalize(text: str) -> str:
    return re.sub(r'\s+', ' ', text.strip())


def _can_resume_dm_worker(
    campaign: Campaign | None,
    account: InstagramAccount | None,
    assignment: CampaignAccount | None,
) -> bool:
    if campaign is None or account is None or assignment is None:
        return False
    if campaign.status not in (CampaignStatus.running, CampaignStatus.scraping_and_running):
        return False
    if account.status not in (AccountStatus.active, AccountStatus.warming_up):
        return False
    return assignment.is_active and assignment.role in ("dm", "both")


async def recover_sending_messages() -> dict:
    from sqlalchemy import select

    cutoff = datetime.utcnow() - timedelta(minutes=10)
    recovered = retried = skipped = errors = 0

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Message).where(
                Message.status == MessageStatus.sending,
                Message.updated_at < cutoff,
            )
        )
        stale = result.scalars().all()

        if not stale:
            logger.debug("[Recovery] No stale 'sending' messages found")
            return {"recovered": 0, "retried": 0, "skipped": 0, "errors": 0}

        logger.info(f"[Recovery] Found {len(stale)} stale 'sending' message(s)")

        for msg in stale:
            try:
                outcome = await _recover_one(msg, db)
                if outcome == "recovered":
                    recovered += 1
                elif outcome == "retried":
                    retried += 1
                elif outcome == "skipped":
                    skipped += 1
                else:
                    errors += 1
            except Exception as exc:
                logger.error(f"[Recovery] Unhandled error for message {msg.id}: {exc}")
                errors += 1

        logger.info(f"[Recovery] Done: recovered={recovered} retried={retried} skipped={skipped} errors={errors}")

    return {"recovered": recovered, "retried": retried, "skipped": skipped, "errors": errors}


async def _recover_one(msg: Message, db) -> str:
    from sqlalchemy import select

    f_res = await db.execute(select(Follower).where(Follower.id == msg.follower_id))
    follower = f_res.scalar_one_or_none()
    if follower is None:
        logger.warning(f"[Recovery] {msg.id}: follower not found -- reset to retry")
        msg.status = MessageStatus.retry
        msg.updated_at = datetime.utcnow()
        await db.commit()
        return "retried"

    account_id = msg.account_id
    if not account_id:
        logger.warning(f"[Recovery] {msg.id}: no account_id -- leaving as sending")
        return "skipped"

    acc_res = await db.execute(select(InstagramAccount).where(InstagramAccount.id == account_id))
    account = acc_res.scalar_one_or_none()
    if not account or account.status in (
        AccountStatus.banned, AccountStatus.disabled, AccountStatus.challenge_required
    ):
        s = account.status.value if account else "not_found"
        logger.warning(f"[Recovery] {msg.id}: account status={s} -- leaving as sending")
        return "skipped"

    try:
        delivered = await _check_dm_delivered(account, follower.ig_user_id, msg.generated_text, db)
    except _InstagrapiParseError as exc:
        # IG returned a media object we can't parse (e.g. MediaXma.video_url=None).
        # Delivery status is UNKNOWN — leave as 'sending' for the next cron pass.
        # Report at most once per account per 30 min to avoid Telegram spam.
        logger.warning(f"[Recovery] {msg.id} @{follower.username}: parse error (inconclusive) — {exc}")
        if _should_report_anomaly(account_id, "dm_recovery_instagrapi_error"):
            try:
                from app.services.anomaly_detector import report_anomaly
                await report_anomaly(
                    db, kind="dm_recovery_instagrapi_error", severity="warn",
                    campaign_id=msg.campaign_id, account_id=account_id,
                    details={
                        "message_id": msg.id,
                        "follower": follower.username,
                        "error": str(exc)[:200],
                        "note": "parse error — delivery unknown, message left as 'sending'",
                    },
                )
            except Exception:
                pass
        return "skipped"
    except Exception as exc:
        logger.warning(f"[Recovery] {msg.id}: instagrapi check failed: {exc}")
        if _should_report_anomaly(account_id, "dm_recovery_instagrapi_error"):
            try:
                from app.services.anomaly_detector import report_anomaly
                await report_anomaly(
                    db, kind="dm_recovery_instagrapi_error", severity="warn",
                    campaign_id=msg.campaign_id, account_id=account_id,
                    details={"message_id": msg.id, "follower": follower.username, "error": str(exc)[:200]},
                )
            except Exception:
                pass
        return "error"

    if delivered:
        msg.status = MessageStatus.sent
        msg.sent_at = msg.sent_at or datetime.utcnow()
        msg.updated_at = datetime.utcnow()
        follower.status = FollowerStatus.sent
        follower.locked_by_account_id = None
        follower.locked_at = None
        db.add(ActivityLog(
            account_id=account_id,
            campaign_id=msg.campaign_id,
            action="dm_recovered",
            details=json.dumps({
                "message_id": msg.id,
                "follower": follower.username,
                "ig_user_id": follower.ig_user_id,
            }),
        ))
        await db.commit()
        logger.info(f"[Recovery] {msg.id} @{follower.username}: DM confirmed -- marked sent")
        await _resume_dm_worker_after_recovery(msg, account, db)
        return "recovered"

    if msg.retry_count >= 1:
        # Già ritentato una volta senza evidenza di consegna → stato TERMINALE.
        # Prima il messaggio restava 'sending' all'infinito e il cron ri-notificava
        # `dm_recovery_no_evidence_repeat` ogni 5 min (spam Telegram). Ora lo si
        # marca failed: la riga esce da 'sending' → il cron non lo ripesca più →
        # una sola notifica. Recuperabile manualmente con l'endpoint retry-failed.
        logger.warning(
            f"[Recovery] {msg.id} @{follower.username}: nessuna evidenza dopo retry "
            "— marco failed (terminale, stop notifiche ripetute)"
        )
        msg.status = MessageStatus.failed
        msg.error_message = "recovery: nessuna evidenza di consegna dopo retry"
        msg.updated_at = datetime.utcnow()
        follower.status = FollowerStatus.failed
        follower.locked_by_account_id = None
        follower.locked_at = None
        from app.services import reservation
        await reservation.release(follower.ig_user_id, db)
        db.add(ActivityLog(
            account_id=account_id,
            campaign_id=msg.campaign_id,
            action="dm_recovery_giveup",
            details=json.dumps({
                "message_id": msg.id,
                "follower": follower.username,
                "ig_user_id": follower.ig_user_id,
                "retry_count": msg.retry_count,
            }),
        ))
        await db.commit()
        emit_event(
            msg.campaign_id,
            "dm_recovery_giveup",
            (
                f"DM non confermato per @{follower.username} dopo il retry: "
                "marcato fallito per evitare retry infiniti."
            ),
            level="warn",
        )
        await _resume_dm_worker_after_recovery(msg, account, db)
        try:
            from app.services.anomaly_detector import report_anomaly
            await report_anomaly(
                db, kind="dm_recovery_giveup", severity="warn",
                campaign_id=msg.campaign_id, account_id=account_id,
                details={
                    "message_id": msg.id,
                    "follower": follower.username,
                    "ig_user_id": follower.ig_user_id,
                    "retry_count": msg.retry_count,
                },
            )
        except Exception as exc:
            logger.warning(f"[Recovery] giveup anomaly report failed: {exc}")
        return "skipped"

    msg.status = MessageStatus.retry
    msg.retry_count += 1
    msg.updated_at = datetime.utcnow()
    follower.status = FollowerStatus.message_generated
    follower.locked_by_account_id = None
    follower.locked_at = None
    # No delivery evidence and message is being retried → release the global
    # contact reservation so this lead can be re-attempted (placeholder leak fix).
    from app.services import reservation
    await reservation.release(follower.ig_user_id, db)
    db.add(ActivityLog(
        account_id=account_id,
        campaign_id=msg.campaign_id,
        action="dm_recovery_no_evidence",
        details=json.dumps({
            "message_id": msg.id,
            "follower": follower.username,
            "ig_user_id": follower.ig_user_id,
            "retry_count": msg.retry_count,
        }),
    ))
    await db.commit()
    emit_event(
        msg.campaign_id,
        "dm_recovery_no_evidence",
        f"DM non confermato per @{follower.username}: rimesso in retry una volta.",
        level="warn",
    )
    logger.info(f"[Recovery] {msg.id} @{follower.username}: no evidence -- reset to retry")
    await _resume_dm_worker_after_recovery(msg, account, db)

    # Notify only — no auto-stop. Indicates a 'sending' message couldn't be confirmed.
    try:
        from app.services.anomaly_detector import report_anomaly
        await report_anomaly(
            db, kind="dm_recovery_no_evidence", severity="warn",
            campaign_id=msg.campaign_id, account_id=account_id,
            details={
                "message_id": msg.id,
                "follower": follower.username,
                "ig_user_id": follower.ig_user_id,
            },
        )
    except Exception as exc:
        logger.warning(f"[Recovery] anomaly report failed: {exc}")

    return "retried"


async def _resume_dm_worker_after_recovery(
    msg: Message,
    account: InstagramAccount,
    db,
) -> None:
    """Restart an account worker that may have died while a DM was in flight."""
    from sqlalchemy import select

    try:
        result = await db.execute(
            select(Campaign, CampaignAccount)
            .join(CampaignAccount, CampaignAccount.campaign_id == Campaign.id)
            .where(
                Campaign.id == msg.campaign_id,
                CampaignAccount.account_id == account.id,
            )
        )
        row = result.first()
        campaign, assignment = row if row else (None, None)
        if not _can_resume_dm_worker(campaign, account, assignment):
            return

        from app.services.work_enqueue import dm_worker_job_exists, reenqueue_one_dm_worker
        from app.utils.events import emit as emit_event

        if await dm_worker_job_exists(msg.campaign_id, account.id):
            logger.info(
                f"[Recovery] Worker already queued/running after message {msg.id}; "
                f"skip requeue for campaign={msg.campaign_id}, account={account.id}"
            )
            return

        await reenqueue_one_dm_worker(msg.campaign_id, account.id, defer_seconds=0)
        emit_event(
            msg.campaign_id,
            "worker_requeued",
            f"Worker DM riaccodato dopo recovery per @{account.username}",
        )
        logger.info(
            f"[Recovery] Requeued DM worker after message {msg.id}: "
            f"campaign={msg.campaign_id}, account={account.id}"
        )
    except Exception as exc:
        # Recovery has already persisted the message outcome. A Redis enqueue
        # failure must not turn a confirmed delivery back into a recovery error.
        logger.warning(
            f"[Recovery] Could not requeue worker after message {msg.id}: {exc}"
        )


async def _check_dm_delivered(account, ig_user_id, generated_text, db) -> bool:
    from pydantic import ValidationError as PydanticValidationError

    client = await _login(account, db, skip_gql_verify=True)
    own_pk = int(client.user_id)
    target_pk = int(ig_user_id)
    normalized_expected = _normalize(generated_text)

    try:
        threads = await asyncio.to_thread(client.direct_threads, amount=100)
    except PydanticValidationError as exc:
        # IG thread list contains a media object that fails pydantic validation
        # (e.g. MediaXma.video_url=None after the monkey-patch also failed or
        # a similar new field). Delivery is UNKNOWN — caller decides what to do.
        raise _InstagrapiParseError(str(exc)) from exc
    except Exception as exc:
        logger.warning(f"[Recovery] direct_threads failed: {exc}")
        raise

    thread = None
    for t in threads:
        for user in t.users:
            if int(user.pk) == target_pk:
                thread = t
                break
        if thread:
            break

    if thread is None:
        logger.debug(f"[Recovery] No thread found with user {ig_user_id}")
        return False

    for msg in thread.messages:
        if not hasattr(msg, "user_id") or msg.user_id is None:
            continue
        if int(msg.user_id) != own_pk:
            continue
        msg_text = getattr(msg, "text", None) or ""
        if _normalize(msg_text) == normalized_expected:
            return True

    return False
