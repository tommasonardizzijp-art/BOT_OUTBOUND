"""
Recovery checker: bonifica i Message rimasti in status='sending'.

Con la marcatura 'sending' spostata a DOPO la pressione di Invio (callback
on_enter in send_dm), un messaggio qui significa: Invio premuto ma il commit
'sent' non e' arrivato (crash/interruzione tra Invio e commit). Caso ormai raro.

IMPORTANTE — nessuna lettura API Instagram. La vecchia verifica di consegna
leggeva `direct_threads` via instagrapi: e' esattamente il "pattern API nudo"
che fa scattare i checkpoint sull'account. Rimossa. Non potendo confermare la
consegna senza API, un 'sending' fermo viene marcato `failed` (terminale):
NIENTE resend automatico -> niente doppioni. Recuperabile a mano con
l'endpoint retry-failed.

Chiamato da:
 - ARQ cron ogni 5 minuti (task_queue.py).
 - Endpoint manuale POST /messages/recover-sending.
"""
import json
from datetime import datetime, timedelta
from loguru import logger

from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount, AccountStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.campaign_account import CampaignAccount
from app.models.follower import Follower, FollowerStatus
from app.models.message import Message, MessageStatus
from app.models.activity_log import ActivityLog
from app.utils.events import emit as emit_event
from app.utils.roles import can_dm


# Dedup table: { (account_id, kind) -> last_reported_at }
# Evita spam di notifiche quando molti 'sending' dello stesso account finiscono qui.
_anomaly_last_reported: dict[tuple[str, str], datetime] = {}
_ANOMALY_DEDUP_SECONDS = 1800  # 30 minutes


def _should_report_anomaly(account_id: str | None, kind: str) -> bool:
    """True se e' passato abbastanza tempo dall'ultimo report di questo kind/account."""
    key = (account_id or "", kind)
    last = _anomaly_last_reported.get(key)
    now = datetime.utcnow()
    if last and (now - last).total_seconds() < _ANOMALY_DEDUP_SECONDS:
        return False
    _anomaly_last_reported[key] = now
    return True


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
    return assignment.is_active and can_dm(assignment.role)


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
                try:
                    await db.rollback()
                except Exception:
                    pass
                errors += 1

        logger.info(f"[Recovery] Done: recovered={recovered} retried={retried} skipped={skipped} errors={errors}")

    return {"recovered": recovered, "retried": retried, "skipped": skipped, "errors": errors}


async def _recover_one(msg: Message, db) -> str:
    """Bonifica UN messaggio fermo in 'sending' — SENZA lettura API (rischio checkpoint).

    Lo marca `failed` (terminale): l'Invio era stato premuto ma la consegna non e'
    confermabile senza API. Nessun resend automatico -> niente doppioni. Libera il
    follower e la reservation. Recuperabile a mano con retry-failed.
    """
    from sqlalchemy import select
    from app.services import reservation

    f_res = await db.execute(select(Follower).where(Follower.id == msg.follower_id))
    follower = f_res.scalar_one_or_none()

    account = None
    if msg.account_id:
        acc_res = await db.execute(
            select(InstagramAccount).where(InstagramAccount.id == msg.account_id)
        )
        account = acc_res.scalar_one_or_none()

    msg.status = MessageStatus.failed
    msg.error_message = (
        "recovery: Invio premuto, consegna non confermata (verifica API disattivata) "
        "— retry manuale se serve"
    )
    msg.updated_at = datetime.utcnow()

    uname = None
    if follower is not None:
        uname = follower.username
        follower.status = FollowerStatus.failed
        follower.locked_by_account_id = None
        follower.locked_at = None
        await reservation.release(follower.ig_user_id, db)

    db.add(ActivityLog(
        account_id=msg.account_id,
        campaign_id=msg.campaign_id,
        action="dm_recovery_giveup",
        details=json.dumps({
            "message_id": msg.id,
            "follower": uname,
            "reason": "no_api_check",
        }),
    ))
    await db.commit()

    logger.warning(
        f"[Recovery] {msg.id} @{uname or msg.follower_id}: 'sending' fermo — "
        "marcato failed (nessuna verifica API)"
    )
    emit_event(
        msg.campaign_id,
        "dm_recovery_giveup",
        (
            f"DM non confermato per @{uname or msg.follower_id}: marcato fallito "
            "(verifica API disattivata, nessun reinvio automatico)."
        ),
        level="warn",
    )

    # Riaccoda il worker DM se la campagna e' ancora attiva (via Redis, no API).
    if account is not None:
        await _resume_dm_worker_after_recovery(msg, account, db)

    if _should_report_anomaly(msg.account_id, "dm_recovery_giveup"):
        try:
            from app.services.anomaly_detector import report_anomaly
            await report_anomaly(
                db, kind="dm_recovery_giveup", severity="warn",
                campaign_id=msg.campaign_id, account_id=msg.account_id,
                details={"message_id": msg.id, "follower": uname, "reason": "no_api_check"},
            )
        except Exception as exc:
            logger.warning(f"[Recovery] giveup anomaly report failed: {exc}")

    return "skipped"


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
        # Il recovery ha gia' persistito l'esito del messaggio. Un fallimento di
        # enqueue Redis non deve propagare.
        logger.warning(
            f"[Recovery] Could not requeue worker after message {msg.id}: {exc}"
        )
