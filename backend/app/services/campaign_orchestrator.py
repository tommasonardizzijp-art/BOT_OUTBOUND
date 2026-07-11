"""
Campaign orchestrator — per-account worker loop.

Architecture (multi-account parallel):
  - One ARQ job per assigned account: run_campaign_task(campaign_id, account_id)
  - Each job calls run_campaign_worker(campaign_id, account_id)
  - Workers claim followers atomically via optimistic locking:
      1. SELECT a random unclaimed follower
      2. UPDATE it WHERE locked_by_account_id IS NULL  (atomic in SQLite WAL)
      3. rowcount == 1 → claimed; 0 → retry
  - Crash recovery: stale locks (older than LOCK_TIMEOUT_MINUTES) are released
    by the recurring cron job AND at the start of each claim attempt.
  - Deduplication: global_contacts table prevents contacting the same IG user twice.
  - Campaign daily limit: workers pause and exit if campaign hits its daily_limit;
    daily cron at midnight resets counts and restarts workers.
  - Account daily limit: worker exits when account hits its effective daily limit;
    daily cron restarts workers next day.
"""
import asyncio
import json
import uuid
from datetime import datetime, timedelta
from arq.worker import Retry
from loguru import logger
from sqlalchemy import select, update, delete, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from app.utils.events import emit as emit_event
from app.utils.roles import can_dm

from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount, AccountStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.campaign_account import CampaignAccount
from app.models.follower import Follower, FollowerStatus
from app.models.message import Message, MessageStatus
from app.models.activity_log import ActivityLog
from app.models.global_contact import GlobalContact
from app.services import account_manager
from app.services import account_lease, reservation
from app.services.account_manager import get_warmup_limit
from app.services.ai_personalizer import compose_message
from app.services.human_behavior import SessionManager
from app.services.anomaly_detector import report_anomaly
from app.services.bot_state_service import is_halted
from app.services.campaign_control import CampaignControlError, ensure_campaign_can_send_messages
from app.config import settings
from app.utils.dm_batch import DmBatchPacer
from app.utils.exceptions import (
    AccountBannedError, AccountChallengeError, AIGenerationTransientError,
    BotHaltedError, DMAbortedBeforeSendError, DMSendError, DMRestrictedError,
)
from app.utils.timing import (
    initial_session_browse_seconds,
    random_delay_seconds,
    distraction_pause_seconds,
    should_take_distraction_pause,
)

# How long a follower can stay locked before considered stale (crashed worker).
# Must be longer than: max_delay_seconds + DM send time + safety margin.
# Production max_delay = 480s (8 min) → 20 min gives plenty of buffer.
LOCK_TIMEOUT_MINUTES = 20


def _gen_backoff_seconds(attempt: int, base: int, cap: int) -> int:
    """Backoff esponenziale per i fallimenti transient di generazione AI.
    attempt 1 → base, 2 → base*2, 3 → base*4 ... con tetto `cap`. Rompe l'hot-loop
    che riclaimava gli stessi follower a delay zero amplificando il 429."""
    if attempt < 1:
        attempt = 1
    return int(min(base * (2 ** (attempt - 1)), cap))


def _cancelled_attempt_is_safe_to_release(message: Message | None) -> bool:
    """Return True while a cancelled worker cannot have pressed Enter yet."""
    return message is None or message.status in (MessageStatus.pending, MessageStatus.retry)


async def _release_cancelled_pending_attempt(
    follower: Follower,
    message: Message | None,
    account_id: str,
    db: AsyncSession,
) -> bool:
    """Release pre-send state only while cancellation is still provably reversible."""
    if not _cancelled_attempt_is_safe_to_release(message):
        return False

    follower_id = follower.id
    ig_user_id = follower.ig_user_id
    await db.rollback()
    await reservation.release(ig_user_id, db)
    await db.execute(
        update(Follower)
        .where(
            Follower.id == follower_id,
            Follower.locked_by_account_id == account_id,
        )
        .values(locked_by_account_id=None, locked_at=None)
    )
    await db.commit()
    return True


# ─────────────────────────── Public entry point ───────────────────────────

async def run_campaign_worker(campaign_id: str, account_id: str) -> None:
    """
    Single-account worker loop. Runs until:
    - Campaign is paused / stopped / completed
    - Account hits its effective daily limit for this campaign
    - Campaign hits its daily_limit
    - No more followers to claim (triggers _maybe_complete_campaign)
    - Account is banned or challenge-required (human intervention needed)

    Browser lifecycle: a single `BrowserSession` is held alive across all DMs of
    one sending session. It is opened lazily on the first DM (with initial
    ambient feed browse), reused for subsequent DMs (with feed ambient between),
    and closed on session break / active-hours wait / any worker exit.
    """
    session_mgr = SessionManager()
    consecutive_failures = 0
    consecutive_unexpected_errors = 0
    consecutive_gen_failures = 0   # transient AI-gen consecutivi → backoff/defer (anti-tempesta 429)
    session_profiles: list[str] = []  # usernames of sent DMs this session
    session_failed: int = 0           # failed DM attempts this session
    session_skipped: int = 0          # skipped (global_contacts dedup) this session
    session_account_username: str | None = None  # set once account is loaded
    job_id = f"worker:{campaign_id}:{account_id}"
    lease_owner = f"{job_id}:{uuid.uuid4().hex[:8]}"
    lease_acquired = False
    release_lease_on_exit = True
    BATCH_DM_BUDGET = settings.session_max_messages
    dm_in_this_invocation = 0
    FAILURE_THRESHOLD = 3
    UNEXPECTED_ERROR_THRESHOLD = 5
    active_follower: Follower | None = None
    active_message: Message | None = None

    # Long-lived browser session (None = closed). Opened on first DM of a
    # session, closed before any long sleep / worker exit.
    browser_session = None  # type: ignore[assignment]

    async def _close_browser() -> None:
        nonlocal browser_session
        if browser_session is not None:
            try:
                await browser_session.close()
            except Exception as e:
                logger.warning(f"[Worker] BrowserSession close failed: {e}")
            browser_session = None

    async def _defer_next_batch(reason: str) -> None:
        nonlocal release_lease_on_exit
        await _close_browser()
        from app.utils.timing import session_break_seconds

        delay = int(session_break_seconds())
        pause_min = max(1, round(delay / 60))
        resume_at_local = datetime.utcnow() + timedelta(
            seconds=delay,
            hours=settings.timezone_offset_hours,
        )
        acct_label = f"@{session_account_username}" if session_account_username else account_id[:8]
        summary = (
            f"{reason}: sessione {acct_label} completata "
            f"({len(session_profiles)} inviati, {session_failed} falliti, {session_skipped} saltati). "
            f"Pausa {pause_min} min, ripartenza prevista circa alle {resume_at_local:%H:%M}."
        )
        # Heartbeat: il defer (pausa sessione, fino a SESSION_BREAK_MAX min) NON è
        # un crash. Aggiorna updated_at così release_stale_locks misura l'inattività
        # dal momento del defer e non auto-pausa una campagna sana.
        try:
            await db.execute(
                update(Campaign).where(Campaign.id == campaign_id).values(updated_at=datetime.utcnow())
            )
            await db.commit()
        except Exception as _hb_e:
            logger.warning(f"[Worker] defer heartbeat fallito: {_hb_e}")
        emit_event(
            campaign_id,
            "session_break",
            summary,
        )
        logger.info(f"[Worker] {summary}")

        if settings.telegram_session_recap_enabled:
            try:
                from app.services.notifier import send_telegram

                profiles_str = (
                    "\n".join(f"  `@{u}`" for u in session_profiles[:10])
                    + (f"\n  ... +{len(session_profiles)-10} altri" if len(session_profiles) > 10 else "")
                ) if session_profiles else "  nessuno"
                await send_telegram(
                    "\n".join(
                        [
                            "*Mini-session recap*",
                            f"Account: `{acct_label}`",
                            f"Inviati: `{len(session_profiles)}`  Falliti: `{session_failed}`  Saltati: `{session_skipped}`",
                            f"Profili contattati:\n{profiles_str}",
                            f"Pausa: `{pause_min} min` - riparte circa alle `{resume_at_local:%H:%M}`",
                        ]
                    ),
                    level="info",
                )
            except Exception as recap_err:
                logger.warning(f"[Worker] Session recap failed: {recap_err}")

        # ARQ-safe defer: re-schedules the current job after `delay` seconds.
        # Enqueueing the same job_id manually from inside the running job is unsafe:
        # ARQ's completion cleanup can zrem that same id and silently lose the next batch.
        try:
            await account_lease.hold_for_seconds(account_id, lease_owner, db, max(1, delay - 5))
            release_lease_on_exit = False
        except Exception as lease_pause_err:
            logger.warning(f"[Worker] pause lease hold failed: {lease_pause_err}")
        raise Retry(defer=delay)

    logger.info(f"[Worker] Started — campaign={campaign_id}, account={account_id}")

    # Pre-stagger status check: exit immediately if campaign is already paused/stopped.
    # Prevents a queued job (enqueued during a previous resume) from sleeping the full
    # stagger window only to find the campaign paused when it wakes up.
    from app.models.campaign import Campaign as _Campaign, CampaignStatus as _CampaignStatus
    async with AsyncSessionLocal() as _pre_db:
        _pre = await _pre_db.execute(select(_Campaign).where(_Campaign.id == campaign_id))
        _pre_camp = _pre.scalar_one_or_none()
        _RUN_STATES = (_CampaignStatus.running, _CampaignStatus.scraping_and_running)
        if not _pre_camp or _pre_camp.status not in _RUN_STATES:
            _s = _pre_camp.status.value if _pre_camp else "not found"
            logger.info(f"[Worker] Campaign {campaign_id} status={_s} at startup — aborting before stagger")
            return
        # Heartbeat: touch updated_at so crash recovery sees this worker attempt.
        _pre_camp.updated_at = datetime.utcnow()
        await _pre_db.commit()

    emit_event(campaign_id, "worker_started", f"Worker avviato per account {account_id[:8]}…")

    # Tiny jitter only: long sleeps inside an ARQ job keep the job marked
    # in-progress and make the worker look stuck. Session pacing happens later.
    import random as _random
    _stagger = _random.uniform(0, 10)
    if _stagger >= 1:
        logger.info(f"[Worker] Startup jitter account={account_id[:8]}: {_stagger:.0f}s")
        await asyncio.sleep(_stagger)

    # Batch invio DM: manda un batch di 1-4 (random) DM CONSECUTIVI, poi il feed
    # browse (che fa anche da riposo anti-ban). Riduce la frequenza dello scroll
    # ~1/batch senza aggiungere attese tra i DM del batch — il browse del profilo
    # target dentro send_dm fa gia' da gap umano. Logica in DmBatchPacer (testata).
    dm_pacer = DmBatchPacer(settings.dm_batch_min, settings.dm_batch_max, _random)

    try:
      async with AsyncSessionLocal() as db:
        while True:
            active_follower = None
            active_message = None

            # Force SQLAlchemy to discard its identity-map cache so the next
            # SELECT always hits the DB. Without this, Campaign.status read
            # within the same session returns the stale object from first load,
            # making pause/stop invisible to the worker until the session ends.
            db.expire_all()

            # ── 0. Global kill-switch ─────────────────────────────────────
            # If anomaly_detector raised the global halt flag, refuse to
            # claim and exit cleanly. Browser closes in `finally`.
            if await is_halted(db):
                logger.warning(f"[Worker] Global BOT_HALTED — exiting (campaign={campaign_id})")
                emit_event(campaign_id, "worker_stopped", "Bot in pausa globale (kill-switch attivo)", level="warn")
                return

            # ── 1. Refresh campaign state ──────────────────────────────────
            result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
            campaign = result.scalar_one_or_none()
            if not campaign:
                logger.error(f"[Worker] Campaign {campaign_id} not found, stopping")
                return
            _DM_RUN_STATES = (CampaignStatus.running, CampaignStatus.scraping_and_running)
            if campaign.status not in _DM_RUN_STATES:
                logger.info(
                    f"[Worker] Campaign {campaign_id} status={campaign.status.value}, stopping"
                )
                emit_event(campaign_id, "worker_stopped", f"Campagna {campaign.status.value} — worker fermato", level="warn")
                return
            try:
                ensure_campaign_can_send_messages(campaign)
            except CampaignControlError as exc:
                logger.warning(f"[Worker] Campaign {campaign_id} cannot send messages: {exc}")
                emit_event(campaign_id, "worker_stopped", str(exc), level="warn")
                return

            # ── 1b. Role check: exit if account's role changed to scraping-only ──
            _ca_result = await db.execute(
                select(CampaignAccount).where(
                    CampaignAccount.campaign_id == campaign_id,
                    CampaignAccount.account_id == account_id,
                )
            )
            _ca = _ca_result.scalar_one_or_none()
            if _ca is None:
                logger.info(f"[Worker] Account {account_id[:8]} unassigned from campaign — stopping DM worker")
                emit_event(campaign_id, "worker_stopped", "Account rimosso dalla campagna — worker fermato", level="warn")
                return
            if not _ca.is_active:
                logger.info(f"[Worker] Account {account_id[:8]} disabled (is_active=False) — stopping DM worker")
                emit_event(campaign_id, "worker_stopped", "Account disabilitato sulla campagna — worker fermato", level="warn")
                return
            if not can_dm(getattr(_ca, 'role', 'both')):
                logger.info(f"[Worker] Account {account_id[:8]} role changed to '{_ca.role}' — stopping DM worker")
                emit_event(campaign_id, "worker_stopped", f"Account ruolo '{_ca.role}' — non abilitato per DM", level="warn")
                return

            # ── 2. Active hours + session break ────────────────────────────
            # Close browser before any long sleep (active-hours wait can be hours,
            # session break is 30-60 min). Idle browser during long pauses is wasted
            # state and looks like an unattended automation if IG telemetry sees it.
            if not lease_acquired:
                lease_acquired = await account_lease.acquire(account_id, lease_owner, db)
                if not lease_acquired:
                    logger.info(f"[Worker] Account {account_id[:8]} already leased by another job, exiting")
                    emit_event(campaign_id, "worker_stopped", "Account gia' in uso da un altro worker", level="warn")
                    return

            if not session_mgr.is_active_hour():
                await _close_browser()
                still = await session_mgr.wait_until_active_hours(campaign_id, db)
                if not still:
                    emit_event(campaign_id, "worker_stopped", "Campagna fermata durante attesa orario attivo", level="warn")
                    return
                # Il lease (TTL 15min) scade durante l'attesa orario attivo (ore).
                # Ri-acquisisci: se un altro job ha preso questo account nel
                # frattempo, esci → un solo worker per account procede.
                lease_acquired = await account_lease.acquire(account_id, lease_owner, db)
                if not lease_acquired:
                    logger.info(f"[Worker] Account {account_id[:8]} preso da altro job dopo attesa orario, esco")
                    emit_event(campaign_id, "worker_stopped", "Account preso da altro worker durante attesa", level="warn")
                    return
            if session_mgr.should_break_session():
                await _defer_next_batch("Fine sessione")
                return
            if False:  # DEAD: blocco session-break legacy, sostituito da _defer_next_batch — unreachable, rimuovere in cleanup
                await _close_browser()
                emit_event(campaign_id, "session_break", "Pausa sessione (simulazione comportamento umano)")
                if settings.telegram_session_recap_enabled:
                    try:
                        from app.services.notifier import send_telegram
                        from app.utils.timing import session_break_seconds as _break_s
                        _pause_s = _break_s()
                        _pause_min = int(_pause_s / 60)
                        _resume_at = datetime.utcnow() + timedelta(seconds=_pause_s)
                        _resume_str = _resume_at.strftime("%H:%M")
                        _acct_label = f"@{session_account_username}" if session_account_username else account_id[:8]
                        _profiles_str = (
                            "\n".join(f"  @{u}" for u in session_profiles[:10])
                            + (f"\n  … +{len(session_profiles)-10} altri" if len(session_profiles) > 10 else "")
                        ) if session_profiles else "  nessuno"
                        _recap = (
                            f"📊 *Mini-session recap*\n"
                            f"Account: {_acct_label}\n"
                            f"✅ Inviati: `{len(session_profiles)}` "
                            f"❌ Falliti: `{session_failed}` "
                            f"⏭️ Saltati: `{session_skipped}`\n"
                            f"Profili contattati:\n{_profiles_str}\n"
                            f"⏸️ Pausa: {_pause_min} min — riprende alle {_resume_str}"
                        )
                        await send_telegram(_recap, level="info")
                    except Exception as recap_err:
                        logger.warning(f"[Worker] Session recap failed: {recap_err}")
                still_running = await session_mgr.take_session_break_interruptible(campaign_id, db)
                session_profiles.clear()
                session_failed = 0
                session_skipped = 0
                dm_pacer.reset()   # nuova sessione dopo il break = nuovo batch
                if not still_running:
                    emit_event(campaign_id, "worker_stopped", "Campagna fermata durante pausa sessione", level="warn")
                    return
                continue

            # ── 3. Validate account still available ────────────────────────
            await account_manager.release_expired_cooldowns(db)

            acc_result = await db.execute(
                select(InstagramAccount).where(InstagramAccount.id == account_id)
            )
            account = acc_result.scalar_one_or_none()
            if not account:
                logger.warning(f"[Worker] Account {account_id} deleted, stopping")
                return
            session_account_username = account.username
            if account.status not in (AccountStatus.active, AccountStatus.warming_up):
                logger.warning(
                    f"[Worker] Account @{account.username} status={account.status.value}, stopping"
                )
                return

            # ── 4. Check per-account-per-campaign daily limit ──────────────
            # Use live DB count from messages table — never stale even if the
            # midnight cron missed firing (worker was down at reset time).
            effective_limit = await _get_effective_daily_limit(account_id, campaign_id, account, db)
            account_sent_today = await _get_account_daily_sent(account_id, db)
            if account_sent_today >= effective_limit:
                logger.info(
                    f"[Worker] @{account.username} hit daily limit "
                    f"({account_sent_today}/{effective_limit}). Worker done for today."
                )
                emit_event(campaign_id, "daily_limit_reached", f"@{account.username} ha raggiunto il limite giornaliero ({account_sent_today}/{effective_limit})", level="warn")
                return  # daily cron at midnight will restart workers

            # ── 5. Check campaign daily limit ──────────────────────────────
            if campaign.daily_limit is not None:
                sent_today = await _get_campaign_daily_sent(campaign_id, db)
                if sent_today >= campaign.daily_limit:
                    logger.info(
                        f"[Worker] Campaign '{campaign.name}' hit daily limit "
                        f"({sent_today}/{campaign.daily_limit}). Worker done for today."
                    )
                    return  # daily cron at midnight will restart workers

            # ── 6. Claim next follower (atomic, multi-worker safe) ─────────
            try:
                follower = await _claim_next_follower(campaign_id, account_id, db)
            except BotHaltedError:
                logger.warning(f"[Worker] Global BOT_HALTED during claim — exiting (campaign={campaign_id})")
                emit_event(campaign_id, "worker_stopped", "Bot in pausa globale (kill-switch attivo)", level="warn")
                return
            if not follower:
                logger.info(f"[Worker] No more unclaimed followers for campaign {campaign_id}")
                emit_event(campaign_id, "no_followers_left", "Nessun follower da processare — campagna completata")
                await _maybe_complete_campaign(campaign_id, db)
                return

            # ── 7. Atomic global contact reservation (BUG-NEW-01 fix) ────────
            # INSERT OR IGNORE on the UNIQUE ig_user_id — if rowcount==0, another
            # worker/campaign has already contacted (or is about to contact) this user.
            # This prevents the TOCTOU race where two workers both pass a SELECT-based
            # check before either has committed to global_contacts.
            reserved = await reservation.try_reserve(follower.ig_user_id, job_id, campaign_id, db)
            if not reserved:
                follower.status = FollowerStatus.skipped
                follower.skip_reason = "already_contacted_globally"
                follower.locked_by_account_id = None
                follower.locked_at = None
                session_skipped += 1
                await db.commit()
                logger.debug(f"[Worker] Skipping @{follower.username} — already in global_contacts")
                continue
            active_follower = follower

            # ── 8. Generate AI message ─────────────────────────────────────
            emit_event(campaign_id, "generating_message", f"Genero messaggio per @{follower.username}…")
            try:
                message = await _get_or_create_message(follower, campaign, db)
            except AIGenerationTransientError as gen_err:
                # Provider AI in rate-limit/timeout (429 ecc). Il follower e' gia'
                # stato rimesso a bio_scraped + sbloccato dentro l'helper. NON fare
                # hot-loop riclaimandolo a delay zero: era cio' che alimentava la
                # tempesta 429. Rilascia la reservation e fai backoff crescente;
                # dopo N transient consecutivi rimanda il batch (riparte piu' tardi).
                await reservation.release(follower.ig_user_id, db)
                await db.commit()
                consecutive_gen_failures += 1
                if consecutive_gen_failures >= settings.ai_gen_failure_threshold:
                    emit_event(
                        campaign_id, "generation_backoff",
                        f"AI sovraccarico dopo {consecutive_gen_failures} tentativi — rimando il batch",
                        level="warn",
                    )
                    logger.warning(
                        f"[Worker] AI transient gen failure #{consecutive_gen_failures} "
                        f"(@{follower.username}): {gen_err} — defer batch"
                    )
                    await _defer_next_batch("AI rate limit")
                    return
                backoff = _gen_backoff_seconds(
                    consecutive_gen_failures,
                    settings.ai_gen_backoff_base_seconds,
                    settings.ai_gen_backoff_cap_seconds,
                )
                emit_event(
                    campaign_id, "generation_backoff",
                    f"AI sovraccarico — pausa {backoff}s prima di riprovare", level="warn",
                )
                logger.warning(
                    f"[Worker] AI transient gen failure #{consecutive_gen_failures} "
                    f"(@{follower.username}): {gen_err} — backoff {backoff}s"
                )
                await asyncio.sleep(backoff)
                continue
            if not message:
                # Generation failed permanently: follower already marked failed inside
                # helper. Release the global contact reservation so other campaigns can try.
                await reservation.release(follower.ig_user_id, db)
                follower.locked_by_account_id = None
                follower.locked_at = None
                await db.commit()
                continue
            consecutive_gen_failures = 0
            active_message = message

            # ── 9. Human-like delay before sending ─────────────────────────
            # If browser is open: ambient feed activity replaces the inter-DM sleep.
            # If browser is closed: skip the sleep — initial ambient browse (step 10)
            # provides the natural delay before the first DM of the session.
            emit_event(campaign_id, "sending_dm", f"Invio DM a @{follower.username} via @{account.username}…")
            # Riposo/scroll SOLO a fine batch (dopo batch_target DM consecutivi).
            # Dentro il batch si tira dritto (attesa 0): il browse del profilo target
            # in send_dm fa gia' da gap umano tra un invio e l'altro.
            if browser_session is not None and dm_pacer.should_browse():
                if should_take_distraction_pause():
                    pause = distraction_pause_seconds()
                    emit_event(
                        campaign_id, "distraction_pause",
                        f"Pausa distrazione {pause/60:.1f} min — browser idle",
                    )
                    logger.info(f"[Worker] Distraction pause {pause:.0f}s with browser idle")
                    await asyncio.sleep(pause)
                else:
                    ambient_dur = random_delay_seconds()
                    try:
                        await browser_session.page.browse_feed(ambient_dur)
                    except Exception as e:
                        logger.warning(f"[Worker] Ambient browse failed, falling back to sleep: {e}")
                        await asyncio.sleep(ambient_dur)
                dm_pacer.record_browse()

            # Re-check status after sleep: campaign may have been paused/stopped
            # while we were waiting. Skip browser open if no longer running.
            _recheck = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
            _camp_now = _recheck.scalar_one_or_none()
            _DM_RUN_STATES = (CampaignStatus.running, CampaignStatus.scraping_and_running)
            if not _camp_now or _camp_now.status not in _DM_RUN_STATES:
                follower.locked_by_account_id = None
                follower.locked_at = None
                await db.commit()
                await reservation.release(follower.ig_user_id, db)
                await db.commit()
                emit_event(campaign_id, "worker_stopped",
                           f"Campagna {_camp_now.status.value if _camp_now else 'rimossa'} — browser non aperto",
                           level="warn")
                logger.info(f"[Worker] Campaign {campaign_id} no longer running after delay, stopping")
                return

            _ca_recheck = await db.execute(
                select(CampaignAccount).where(
                    CampaignAccount.campaign_id == campaign_id,
                    CampaignAccount.account_id == account_id,
                )
            )
            _ca_now = _ca_recheck.scalar_one_or_none()
            if _ca_now is None or not _ca_now.is_active or not can_dm(getattr(_ca_now, 'role', 'both')):
                follower.locked_by_account_id = None
                follower.locked_at = None
                await db.commit()
                await reservation.release(follower.ig_user_id, db)
                await db.commit()
                _reason = "rimosso" if _ca_now is None else ("disabilitato" if not _ca_now.is_active else f"ruolo '{_ca_now.role}'")
                emit_event(campaign_id, "worker_stopped",
                           f"Account @{account.username} {_reason} durante attesa — DM annullato", level="warn")
                logger.info(f"[Worker] Account {account_id[:8]} {_reason} after delay, stopping")
                return

            # ── 10. Send DM ────────────────────────────────────────────────
            # dm_sent_ok flag: True after send_dm returns without exception.
            # Used by catch-all handler to avoid releasing global_contact reservation
            # for a DM that was actually delivered — preventing duplicate sends.
            dm_sent_ok = False
            try:
                # Open browser session lazily on the first DM. Keep alive across
                # subsequent DMs of this session — closed only at session break,
                # active-hours wait, or worker exit (via `_close_browser` in finally).
                if browser_session is None:
                    from app.browser.context_manager import BrowserSession
                    browser_session = await BrowserSession(account_id).open()
                    await browser_session.page.ensure_logged_in(account_id)
                    ambient_init = initial_session_browse_seconds()
                    emit_event(
                        campaign_id, "ambient_browse",
                        f"Browse feed iniziale {ambient_init:.0f}s prima del 1° DM",
                    )
                    try:
                        await browser_session.page.browse_feed(ambient_init)
                    except Exception as e:
                        logger.warning(f"[Worker] Initial ambient browse failed (non-fatal): {e}")

                # Final kill-switch gate before the only irreversible action path.
                # If halt lands during browser setup or ambient browse, release the
                # lead and global reservation while the message is still pending.
                if await is_halted(db):
                    follower.locked_by_account_id = None
                    follower.locked_at = None
                    await reservation.release(follower.ig_user_id, db)
                    await db.commit()
                    emit_event(campaign_id, "worker_stopped", "Bot in pausa globale prima dell'invio DM", level="warn")
                    logger.warning(
                        f"[Worker] Global BOT_HALTED before send_dm; released @{follower.username} "
                        f"(campaign={campaign_id})"
                    )
                    return

                # 'sending' viene marcato SOLO dopo che l'Invio e' stato premuto,
                # tramite la callback on_enter passata a send_dm (il "punto di non
                # ritorno"). NON marcarlo prima: se send_dm fallisce PRIMA dell'Invio
                # (es. textbox non trovato/non cliccabile), il messaggio resta in
                # 'message_generated' -> il catch-all fa un retry pulito, il messaggio
                # NON resta appeso in 'sending' e il recovery non deve leggere l'API.
                async def _mark_sending() -> None:
                    message.status = MessageStatus.sending
                    message.account_id = account_id
                    message.updated_at = datetime.utcnow()
                    await db.commit()

                # Pre-send callback: re-check follower status just before pressing Enter.
                # Returns False if another worker already processed this lead while we
                # were setting up the browser -- abort before DM is delivered.
                async def _pre_send_check() -> bool:
                    if await is_halted(db):
                        logger.warning(f"[Worker] Global BOT_HALTED inside pre-send callback for @{follower.username}")
                        return False
                    from sqlalchemy import select as _select
                    _res = await db.execute(
                        _select(Follower).where(Follower.id == follower.id)
                    )
                    _f = _res.scalar_one_or_none()
                    if _f is None:
                        return False
                    return _f.status in (FollowerStatus.bio_scraped, FollowerStatus.message_generated)

                await browser_session.page.send_dm(
                    username=follower.username,
                    message=message.generated_text,
                    pre_send_callback=_pre_send_check,
                    on_enter=_mark_sending,
                )

                # Mark as delivered IMMEDIATELY: persist follower.status=sent + message.sent
                # before any other operation that could throw. Single source of truth
                # avoids resend if a downstream call fails.
                dm_sent_ok = True
                follower.status = FollowerStatus.sent
                follower.locked_by_account_id = None
                follower.locked_at = None
                message.status = MessageStatus.sent
                message.sent_at = datetime.utcnow()
                message.account_id = account_id
                await db.commit()

                # Success bookkeeping (any failure here does NOT cause resend — DM is committed)
                try:
                    await account_manager.record_success(account_id, db)
                except Exception as e:
                    logger.warning(f"[Worker] record_success failed post-send (non-fatal): {e}")
                    await db.rollback()

                try:
                    await _mark_globally_contacted(
                        follower.ig_user_id, campaign_id, db,
                        follower=follower, account=account, campaign_name=campaign.name,
                    )
                    await reservation.release(follower.ig_user_id, db)
                except Exception as e:
                    logger.warning(f"[Worker] _mark_globally_contacted failed post-send (non-fatal): {e}")
                    await db.rollback()

                campaign.messages_sent += 1
                campaign.messages_pending = max(0, campaign.messages_pending - 1)
                campaign.updated_at = datetime.utcnow()
                try:
                    await db.commit()
                except Exception as e:
                    logger.warning(f"[Worker] campaign counter commit failed post-send (non-fatal): {e}")
                    await db.rollback()

                session_mgr.record_message_sent()
                dm_in_this_invocation += 1
                dm_pacer.record_sent()   # DM riuscito conta nel batch corrente
                session_profiles.append(follower.username)
                await account_lease.heartbeat(account_id, lease_owner, db)
                consecutive_failures = 0
                consecutive_unexpected_errors = 0

                log = ActivityLog(
                    account_id=account_id,
                    campaign_id=campaign_id,
                    action="dm_sent",
                    details=json.dumps({"follower": follower.username}),
                )
                db.add(log)
                await db.commit()
                emit_event(campaign_id, "dm_sent", f"✓ DM inviato a @{follower.username}")
                logger.info(f"[Worker] DM sent to @{follower.username} via @{account.username}")

                if session_mgr.should_break_session() or dm_in_this_invocation >= BATCH_DM_BUDGET:
                    await _defer_next_batch("Fine batch")
                    return

            except Retry:
                raise

            except DMRestrictedError:
                # Release reservation: DM-restricted user, no point blocking other campaigns
                try:
                    await reservation.release(follower.ig_user_id, db)
                    follower.status = FollowerStatus.skipped
                    follower.skip_reason = "dm_restricted"
                    follower.locked_by_account_id = None
                    follower.locked_at = None
                    message.status = MessageStatus.failed
                    message.error_message = "User has DM restrictions"
                    campaign.messages_failed += 1
                    campaign.messages_pending = max(0, campaign.messages_pending - 1)
                    session_skipped += 1
                    await db.commit()
                except Exception as _db_err:
                    logger.warning(f"[Worker] DB cleanup failed after DMRestrictedError: {_db_err}")
                emit_event(campaign_id, "dm_restricted", f"@{follower.username} non accetta DM — saltato", level="warn")
                logger.info(f"[Worker] @{follower.username} has DM restrictions, skipping")

            except AccountChallengeError as e:
                # Release reservation: account-side issue, another account can try this user
                try:
                    await reservation.release(follower.ig_user_id, db)
                    acc_res = await db.execute(
                        select(InstagramAccount).where(InstagramAccount.id == e.account_id)
                    )
                    acc = acc_res.scalar_one_or_none()
                    if acc:
                        acc.status = AccountStatus.challenge_required
                    follower.locked_by_account_id = None
                    follower.locked_at = None
                    await db.commit()
                except Exception as _db_err:
                    logger.warning(f"[Worker] DB cleanup failed after AccountChallengeError: {_db_err}")
                emit_event(campaign_id, "account_challenge", f"@{account.username} richiede verifica — worker fermo", level="error")
                logger.error(f"[Worker] Challenge required for @{account.username}. Worker stopping.")
                if browser_session and hasattr(browser_session, "page"):
                    try:
                        from app.services.notifier import capture_and_send_screenshot
                        await capture_and_send_screenshot(
                            browser_session.page,
                            label=f"challenge_{account.username}",
                            caption=(
                                f"Instagram chiede una verifica per @{account.username}. "
                                "Worker fermo finche' non viene completata."
                            ),
                            level="error",
                        )
                    except Exception:
                        pass
                await report_anomaly(
                    db, kind="challenge", severity="error",
                    campaign_id=campaign_id, account_id=e.account_id or account_id,
                    details={"username": account.username},
                )
                return  # Human must resolve challenge

            except AccountBannedError:
                # Release reservation: account-side issue, another account can try this user
                try:
                    await reservation.release(follower.ig_user_id, db)
                    await account_manager.mark_banned(account_id, db)
                    follower.locked_by_account_id = None
                    follower.locked_at = None
                    await db.commit()
                except Exception as _db_err:
                    logger.warning(f"[Worker] DB cleanup failed after AccountBannedError: {_db_err}")
                emit_event(campaign_id, "account_banned", f"@{account.username} BANNATO — worker fermo", level="error")
                logger.error(f"[Worker] @{account.username} banned. Worker stopping.")
                if browser_session and hasattr(browser_session, "page"):
                    try:
                        from app.services.notifier import capture_and_send_screenshot
                        await capture_and_send_screenshot(
                            browser_session.page,
                            label=f"banned_{account.username}",
                            caption=(
                                f"Account @{account.username} risulta bannato. "
                                "Non verra' usato per altri invii."
                            ),
                            level="critical",
                        )
                    except Exception:
                        pass
                await report_anomaly(
                    db, kind="account_banned", severity="critical",
                    campaign_id=campaign_id, account_id=account_id,
                    details={"username": account.username},
                )
                return  # Banned — no point continuing

            except DMAbortedBeforeSendError as e:
                # Callback aborted BEFORE pressing Enter: DM was NOT delivered.
                # message.status was set to 'sending' but Enter was never pressed.
                # Reset message back to 'pending' (or 'retry') and release lock.
                # Do NOT keep dm_sent_ok=True (it's still False here).
                logger.warning(
                    f"[Worker] Pre-send check aborted DM for @{follower.username} "
                    f"(another worker likely claimed this follower): {e}"
                )
                try:
                    await reservation.release(follower.ig_user_id, db)
                    message.status = MessageStatus.pending
                    message.updated_at = datetime.utcnow()
                    follower.locked_by_account_id = None
                    follower.locked_at = None
                    await db.commit()
                except Exception as _db_err:
                    logger.warning(f"[Worker] DB cleanup failed after DMAbortedBeforeSendError: {_db_err}")
                continue  # Try next follower

            except DMSendError as e:
                # Guard: if DM was already delivered (flag set), DON'T release reservation
                # — preventing the resend loop that caused 4x duplicate DMs.
                if dm_sent_ok:
                    logger.warning(f"[Worker] DMSendError after successful send for @{follower.username} — ignoring (DM delivered)")
                    continue

                # Release reservation so this follower can be retried (lock also released below)
                await reservation.release(follower.ig_user_id, db)
                await account_manager.record_failure(account_id, db, str(e))
                message.retry_count += 1
                consecutive_failures += 1
                consecutive_unexpected_errors = 0  # DMSendError = proxy is up, reset network counter

                if consecutive_failures >= FAILURE_THRESHOLD:
                    await account_manager.apply_cooldown(account_id, db, tier=0)
                    try:
                        from app.services.campaign_control import pause_campaigns_without_usable_dm_accounts
                        paused = await pause_campaigns_without_usable_dm_accounts(db, account_id)
                        if paused:
                            emit_event(
                                campaign_id,
                                "account_cooldown",
                                f"@{account.username} in cooldown: {paused} campagna/e senza altri account DM messe in pausa",
                                level="warn",
                            )
                    except Exception as pause_err:
                        logger.warning(f"[Worker] Failed to pause stranded campaigns after cooldown: {pause_err}")
                    follower.locked_by_account_id = None
                    follower.locked_at = None
                    await db.commit()
                    emit_event(campaign_id, "cooldown_started", f"@{account.username} in cooldown dopo {consecutive_failures} errori consecutivi", level="warn")
                    logger.warning(
                        f"[Worker] {consecutive_failures} consecutive failures. "
                        f"@{account.username} in cooldown. Worker stopping."
                    )
                    return  # Account in cooldown — stop this worker

                if message.retry_count >= 3:
                    follower.status = FollowerStatus.failed
                    follower.locked_by_account_id = None
                    follower.locked_at = None
                    message.status = MessageStatus.failed
                    message.error_message = str(e)
                    campaign.messages_failed += 1
                    campaign.messages_pending = max(0, campaign.messages_pending - 1)
                    session_failed += 1
                    await db.commit()
                    emit_event(campaign_id, "dm_failed", f"✗ DM fallito per @{follower.username}: {str(e)[:80]}", level="error")
                    fail_log = ActivityLog(
                        account_id=account_id,
                        campaign_id=campaign_id,
                        action="dm_failed",
                        details=json.dumps({"follower": follower.username, "error": str(e)[:120]}),
                    )
                    db.add(fail_log)
                    await db.commit()
                    # dm_failed_streak detection: if last 5 messages for this (campaign, account)
                    # are all failed → fire anomaly so orchestrator can auto-pause this campaign.
                    try:
                        recent = await db.execute(
                            select(Message.status)
                            .where(
                                Message.campaign_id == campaign_id,
                                Message.account_id == account_id,
                                Message.status.in_((MessageStatus.sent, MessageStatus.failed)),
                            )
                            .order_by(Message.updated_at.desc())
                            .limit(5)
                        )
                        statuses = [s for (s,) in recent.all()]
                        if len(statuses) >= 5 and all(s == MessageStatus.failed for s in statuses):
                            await report_anomaly(
                                db, kind="dm_failed_streak", severity="error",
                                campaign_id=campaign_id, account_id=account_id,
                                details={
                                    "username": account.username,
                                    "last_error": str(e)[:200],
                                    "streak_size": 5,
                                },
                            )
                    except Exception as anomaly_err:
                        logger.warning(f"[Worker] dm_failed_streak check failed: {anomaly_err}")
                else:
                    # Release lock so this follower can be retried next iteration
                    follower.locked_by_account_id = None
                    follower.locked_at = None
                    await db.commit()

            except Exception as e:
                logger.exception(f"[Worker] Unexpected error for @{follower.username}: {e}")
                emit_event(campaign_id, "worker_error", f"Errore inatteso per @{follower.username}: {str(e)[:120]}", level="error")

                # Guard: if DM was already delivered (flag set), DO NOT release reservation
                # and DO NOT mark follower as failed — DM was sent, error is post-send bookkeeping.
                if dm_sent_ok:
                    logger.warning(f"[Worker] Post-send error for @{follower.username} — DM delivered, skipping cleanup")
                    try:
                        await db.rollback()
                    except Exception:
                        pass
                    continue

                # Guard: if message.status is 'sending', the Enter key may have been
                # pressed before the crash. Do NOT release the global contact reservation
                # (would allow duplicate send). Leave status='sending' for recovery_checker.
                # The stale-lock cron will release the follower lock after LOCK_TIMEOUT_MINUTES.
                if message and message.status == MessageStatus.sending:
                    logger.warning(
                        f"[Worker] Unexpected error for @{follower.username} while status='sending' — "
                        "Enter may have been pressed. Leaving status='sending' for recovery_checker. "
                        f"Error: {e}"
                    )
                    follower.locked_by_account_id = None
                    follower.locked_at = None
                    try:
                        await db.commit()
                    except Exception:
                        await db.rollback()
                    continue

                consecutive_unexpected_errors += 1
                await reservation.release(follower.ig_user_id, db)
                if message:
                    message.retry_count += 1
                    if message.retry_count >= 3:
                        follower.status = FollowerStatus.failed
                        message.status = MessageStatus.failed
                        message.error_message = str(e)[:500]
                        campaign.messages_failed += 1
                        campaign.messages_pending = max(0, campaign.messages_pending - 1)
                follower.locked_by_account_id = None
                follower.locked_at = None
                try:
                    await db.commit()
                except Exception:
                    await db.rollback()
                if consecutive_unexpected_errors >= UNEXPECTED_ERROR_THRESHOLD:
                    campaign_paused = False
                    try:
                        pause_result = await db.execute(
                            update(Campaign)
                            .where(
                                Campaign.id == campaign_id,
                                Campaign.status.in_(
                                    (CampaignStatus.running, CampaignStatus.scraping_and_running)
                                ),
                            )
                            .values(status=CampaignStatus.paused, updated_at=datetime.utcnow())
                        )
                        campaign_paused = (pause_result.rowcount or 0) > 0
                        if campaign_paused:
                            db.add(
                                ActivityLog(
                                    campaign_id=campaign_id,
                                    account_id=account_id,
                                    action="campaign_auto_paused",
                                    details=json.dumps(
                                        {
                                            "reason": "consecutive_unexpected_errors",
                                            "count": consecutive_unexpected_errors,
                                            "username": account.username,
                                            "last_error": str(e)[:200],
                                        }
                                    ),
                                )
                            )
                        await db.commit()
                    except Exception as pause_err:
                        await db.rollback()
                        logger.warning(f"[Worker] Auto-pause commit failed: {pause_err}")
                    emit_event(
                        campaign_id, "worker_error",
                        (
                            "Campagna messa in pausa automaticamente: "
                            f"{consecutive_unexpected_errors} errori tecnici consecutivi. "
                            "Possibile problema di proxy, connessione o sessione Instagram."
                        )
                        if campaign_paused
                        else (
                            f"Worker fermo dopo {consecutive_unexpected_errors} errori tecnici consecutivi. "
                            "La campagna era gia' non-running o la pausa non e' stata applicata."
                        ),
                        level="error",
                    )
                    if campaign_paused:
                        emit_event(
                            campaign_id,
                            "campaign_auto_paused",
                            (
                                "Campagna messa in pausa dopo "
                                f"{consecutive_unexpected_errors} errori tecnici consecutivi."
                            ),
                            level="error",
                        )
                    logger.error(
                        f"[Worker] {consecutive_unexpected_errors} consecutive unexpected errors. "
                        "Auto-paused campaign to protect contact list."
                    )
                    await report_anomaly(
                        db, kind="consecutive_unexpected_errors", severity="error",
                        campaign_id=campaign_id, account_id=account_id,
                        details={
                            "count": consecutive_unexpected_errors,
                            "username": account.username,
                            "last_error": str(e)[:200],
                        },
                    )
                    return
    except asyncio.CancelledError:
        if active_follower is not None:
            active_username = active_follower.username
            try:
                released = await _release_cancelled_pending_attempt(
                    active_follower,
                    active_message,
                    account_id,
                    db,
                )
                if released:
                    emit_event(
                        campaign_id,
                        "worker_stopped",
                        f"Worker interrotto prima dell'invio a @{active_username}; lead liberato",
                        level="warn",
                    )
                    logger.warning(
                        f"[Worker] Cancelled before send for @{active_username}; "
                        "released follower lock and reservation"
                    )
            except Exception as cleanup_err:
                logger.warning(f"[Worker] Pre-send cancellation cleanup failed: {cleanup_err}")
        raise
    finally:
        await _close_browser()
        if lease_acquired and release_lease_on_exit:
            try:
                async with AsyncSessionLocal() as _lease_db:
                    await account_lease.release(account_id, lease_owner, _lease_db)
            except Exception as _lease_err:
                logger.warning(f"[Worker] account_lease.release failed (DB unreachable?): {_lease_err}")


# ─────────────────────────── Follower claiming ────────────────────────────

async def _claim_next_follower(
    campaign_id: str,
    account_id: str,
    db: AsyncSession,
    max_attempts: int = 5,
) -> Follower | None:
    """
    Atomically claim the next available follower for this worker.

    Uses optimistic locking: SELECT a candidate, then UPDATE WHERE still unclaimed.
    SQLite WAL mode serializes writes → safe across concurrent workers.

    Also releases stale locks from crashed workers before selecting candidates.
    """
    if await is_halted(db):
        logger.warning(f"[Claim] Global BOT_HALTED - refusing to claim follower for campaign={campaign_id}")
        raise BotHaltedError("global kill-switch active")

    # Release stale locks from crashed workers
    stale_cutoff = datetime.utcnow() - timedelta(minutes=LOCK_TIMEOUT_MINUTES)
    await db.execute(
        update(Follower)
        .where(
            Follower.campaign_id == campaign_id,
            Follower.locked_by_account_id.isnot(None),
            Follower.locked_at < stale_cutoff,
        )
        .values(locked_by_account_id=None, locked_at=None)
    )
    await db.commit()

    for attempt in range(max_attempts):
        # Find an unclaimed candidate without ORDER BY random() full scan.
        base = (
            select(Follower)
            .where(
                Follower.campaign_id == campaign_id,
                Follower.status.in_([FollowerStatus.bio_scraped, FollowerStatus.message_generated]),
                Follower.locked_by_account_id.is_(None),
            )
        )
        n = await db.scalar(select(func.count()).select_from(base.subquery()))
        if not n:
            return None
        import random as _r
        offset = _r.randint(0, min(n, 500) - 1)
        result = await db.execute(base.offset(offset).limit(1))
        follower = result.scalar_one_or_none()
        if follower is None:
            continue  # Riga claimata tra COUNT e SELECT — riprova prossimo attempt

        # Attempt atomic claim: only succeeds if still unclaimed
        claim = await db.execute(
            update(Follower)
            .where(
                Follower.id == follower.id,
                Follower.locked_by_account_id.is_(None),
            )
            .values(
                locked_by_account_id=account_id,
                locked_at=datetime.utcnow(),
            )
        )
        await db.commit()

        if claim.rowcount == 1:
            # Claimed successfully — refresh to get the committed state
            await db.refresh(follower)

            # Sanity check: if any Message for this follower is already 'sent' or 'sending',
            # the DM was delivered (or is in-flight). Do NOT reprocess.
            # - 'sent': reconcile follower to sent, skip.
            # - 'sending': DM may have been delivered but not confirmed; leave for
            #   recovery_checker.py to reconcile. Log warning and skip.
            already_sent = await db.scalar(
                select(func.count(Message.id)).where(
                    Message.follower_id == follower.id,
                    Message.status == MessageStatus.sent,
                )
            )
            if already_sent and already_sent > 0:
                logger.warning(
                    f"[Claim] Follower {follower.id} (@{follower.username}) has {already_sent} sent message(s) — "
                    f"reconciling status to 'sent' and skipping"
                )
                follower.status = FollowerStatus.sent
                follower.locked_by_account_id = None
                follower.locked_at = None
                await db.commit()
                continue

            already_sending = await db.scalar(
                select(func.count(Message.id)).where(
                    Message.follower_id == follower.id,
                    Message.status == MessageStatus.sending,
                )
            )
            if already_sending and already_sending > 0:
                logger.warning(
                    f"[Claim] Follower {follower.id} (@{follower.username}) has a 'sending' message — "
                    "DM may have been delivered but not confirmed. Skipping; recovery_checker will reconcile."
                )
                follower.locked_by_account_id = None
                follower.locked_at = None
                await db.commit()
                continue

            return follower

        # Another worker claimed it first — try next candidate
        logger.debug(f"[Claim] Contention on follower {follower.id}, retrying ({attempt + 1}/{max_attempts})")

    return None  # All attempts contested — no followers available right now


async def release_stale_locks(db: AsyncSession) -> int:
    """
    Release all follower locks older than LOCK_TIMEOUT_MINUTES.
    Called by the cron job every 15 minutes as a safety net.
    Returns number of locks released.
    """
    stale_cutoff = datetime.utcnow() - timedelta(minutes=LOCK_TIMEOUT_MINUTES)
    result = await db.execute(
        update(Follower)
        .where(
            Follower.locked_by_account_id.isnot(None),
            Follower.locked_at < stale_cutoff,
        )
        .values(locked_by_account_id=None, locked_at=None)
    )
    await db.commit()
    if result.rowcount > 0:
        logger.info(f"[Cron] Released {result.rowcount} stale follower locks")
    return result.rowcount


# ──────────────────────────── Limit helpers ───────────────────────────────

async def _get_effective_daily_limit(
    account_id: str,
    campaign_id: str,
    account: InstagramAccount,
    db: AsyncSession,
) -> int:
    """
    Effective daily limit for this account on this specific campaign.
    Priority: campaign_accounts.daily_limit_override → account warmup-adjusted limit.
    """
    ca_result = await db.execute(
        select(CampaignAccount).where(
            CampaignAccount.campaign_id == campaign_id,
            CampaignAccount.account_id == account_id,
        )
    )
    ca = ca_result.scalar_one_or_none()
    base_limit = ca.daily_limit_override if (ca and ca.daily_limit_override is not None) else account.daily_message_limit

    from app.services.account_manager import apply_safety_caps
    age_days = (datetime.utcnow() - account.created_at).days if account.created_at else 999
    return apply_safety_caps(
        base_limit=base_limit,
        warmup_day=account.warmup_day,
        account_age_days=age_days,
        default_limit=account.daily_message_limit,
        total_messages_sent=account.total_messages_sent or 0,
    )


async def _get_campaign_daily_sent(campaign_id: str, db: AsyncSession) -> int:
    """Count DMs successfully sent today for this campaign (live query, no stale counter)."""
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    result = await db.execute(
        select(func.count(Message.id))
        .where(
            Message.campaign_id == campaign_id,
            Message.status == MessageStatus.sent,
            Message.sent_at >= today_start,
        )
    )
    return result.scalar_one() or 0


async def _get_account_daily_sent(account_id: str, db: AsyncSession) -> int:
    """Count DMs sent today by this account across ALL campaigns (live, never stale).
    Replaces account.daily_message_count which can be stale if the midnight cron missed."""
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    result = await db.execute(
        select(func.count(Message.id))
        .where(
            Message.account_id == account_id,
            Message.status == MessageStatus.sent,
            Message.sent_at >= today_start,
        )
    )
    return result.scalar_one() or 0


# ─────────────────────────── Campaign completion ──────────────────────────

async def _maybe_complete_campaign(campaign_id: str, db: AsyncSession) -> None:
    """
    Mark campaign completed only when no followers remain to be processed AND
    no other worker holds a lock (i.e., nothing is in-flight).
    Safe to call from multiple workers simultaneously.

    BUG-NEW-04 fix: uses atomic UPDATE WHERE instead of SELECT+check+UPDATE to
    eliminate the TOCTOU race between concurrent workers finishing at the same time.
    Only the worker whose UPDATE returns rowcount==1 logs the completion event.
    """
    remaining = await db.scalar(
        select(func.count(Follower.id))
        .where(
            Follower.campaign_id == campaign_id,
            or_(
                Follower.status.in_([FollowerStatus.bio_scraped, FollowerStatus.message_generated, FollowerStatus.pending_approval]),
                Follower.locked_by_account_id.isnot(None),
            )
        )
    )

    if remaining == 0:
        now = datetime.utcnow()
        result = await db.execute(
            update(Campaign)
            .where(
                Campaign.id == campaign_id,
                Campaign.status == CampaignStatus.running,
            )
            .values(
                status=CampaignStatus.completed,
                completed_at=now,
                updated_at=now,
            )
        )
        await db.commit()

        if result.rowcount == 1:
            # We were the one worker that atomically completed it — log it
            campaign_result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
            campaign = campaign_result.scalar_one_or_none()
            name = campaign.name if campaign else campaign_id
            log = ActivityLog(campaign_id=campaign_id, action="campaign_completed")
            db.add(log)
            await db.commit()
            logger.info(f"[Orchestrator] Campaign '{name}' completed!")


# ─────────────────────────── Shared helpers ───────────────────────────────

async def _get_or_create_message(
    follower: Follower, campaign: Campaign, db: AsyncSession
) -> Message | None:
    """Get existing pending/retry message or generate a new one via Ollama.
    M10: if campaign has message_template_b, randomly assigns variant 'a' or 'b' (50/50).
    """
    result = await db.execute(
        select(Message).where(
            Message.follower_id == follower.id,
            Message.status.in_([MessageStatus.pending, MessageStatus.retry]),
        )
    )
    message = result.scalar_one_or_none()
    if message:
        return message

    try:
        text, variant = await compose_message(follower=follower, campaign=campaign)
        message = Message(
            campaign_id=campaign.id,
            follower_id=follower.id,
            generated_text=text,
            status=MessageStatus.pending,
            template_variant=variant,
        )
        db.add(message)
        # Approval is a preview/pre-generation gate only. Once the operator
        # approves the sample, live DM workers must not require per-message review.
        follower.status = FollowerStatus.message_generated
        await db.commit()
        await db.refresh(message)
        return message
    except AIGenerationTransientError:
        raise
    except Exception as e:
        msg = str(e).lower()
        transient = any(k in msg for k in ("429", "rate", "timeout", "timed out", "connect", "temporarily"))
        if transient:
            logger.warning(
                f"AI transient error per @{follower.username} ({e}) — "
                "follower lasciato in bio_scraped per retry"
            )
            follower.status = FollowerStatus.bio_scraped
            follower.locked_by_account_id = None
            follower.locked_at = None
            await db.commit()
            # Segnala al worker: backoff, NON riprovare a raffica (hot-loop 429).
            raise AIGenerationTransientError(str(e)[:200])
        logger.error(f"Failed to generate message for @{follower.username}: {e}")
        follower.status = FollowerStatus.failed
        follower.locked_by_account_id = None
        follower.locked_at = None
        await db.commit()
        return None


async def _send_dm(
    account_id: str, follower: Follower, message: Message, db: AsyncSession
) -> None:
    from app.services.dm_sender import send_dm
    await send_dm(
        account_id=account_id,
        username=follower.username,
        message_text=message.generated_text,
    )


async def _legacy_global_contact_placeholder(ig_user_id: int, db: AsyncSession) -> bool:
    """
    Atomically reserve a global contact slot BEFORE sending a DM.

    Uses INSERT OR IGNORE on the UNIQUE ig_user_id column — SQLite WAL serializes
    writes, so exactly one worker wins the insert. Returns True if this worker
    successfully reserved the slot (first contact), False if already claimed.

    The placeholder row is either:
    - Updated with full details by _mark_globally_contacted() on send success
    - Deleted on send failure
    """
    result = await db.execute(
        _upsert_ignore(
            GlobalContact,
            {
                "id": str(uuid.uuid4()),
                "ig_user_id": ig_user_id,
                "contacted_by_campaign_ids": "[]",
                "contact_history": "[]",
                "created_at": datetime.utcnow(),
            },
            "ig_user_id",
            settings.database_url,
        )
    )
    await db.commit()
    return result.rowcount == 1


async def _legacy_release_placeholder(ig_user_id: int, db: AsyncSession) -> None:
    """
    Delete the placeholder inserted by the legacy reservation helper when a send fails.
    This allows other campaigns/workers to attempt contacting this user in the future.
    NOTE: does NOT commit — caller is responsible for the next commit().
    """
    await db.execute(
        delete(GlobalContact).where(GlobalContact.ig_user_id == ig_user_id)
    )


async def _mark_globally_contacted(
    ig_user_id: int,
    campaign_id: str,
    db: AsyncSession,
    *,
    follower=None,
    account=None,
    campaign_name: str | None = None,
) -> None:
    now = datetime.utcnow()
    history_entry = {
        "campaign_id": campaign_id,
        "campaign_name": campaign_name or "",
        "account_id": account.id if account else None,
        "account_username": account.username if account else None,
        "contacted_at": now.isoformat(),
    }

    result = await db.execute(
        select(GlobalContact).where(GlobalContact.ig_user_id == ig_user_id)
    )
    contact = result.scalar_one_or_none()
    if contact:
        ids = json.loads(contact.contacted_by_campaign_ids)
        if campaign_id not in ids:
            ids.append(campaign_id)
        contact.contacted_by_campaign_ids = json.dumps(ids)
        history = json.loads(contact.contact_history)
        history.append(history_entry)
        contact.contact_history = json.dumps(history)
        contact.last_contacted_at = now
        if follower:
            contact.username = follower.username
            if follower.full_name:
                contact.full_name = follower.full_name
            if follower.biography:
                contact.biography = follower.biography
            # Merge contact fields (fill gaps; don't clobber existing values).
            for field_name in ("phone", "email", "whatsapp", "external_url"):
                fv = getattr(follower, field_name, None)
                if fv and not getattr(contact, field_name, None):
                    setattr(contact, field_name, fv)
            if getattr(follower, "bio_links", None) and not contact.bio_links:
                contact.bio_links = follower.bio_links
    else:
        contact = GlobalContact(
            ig_user_id=ig_user_id,
            username=follower.username if follower else None,
            full_name=follower.full_name if follower else None,
            biography=follower.biography if follower else None,
            phone=getattr(follower, "phone", None) if follower else None,
            email=getattr(follower, "email", None) if follower else None,
            whatsapp=getattr(follower, "whatsapp", None) if follower else None,
            external_url=getattr(follower, "external_url", None) if follower else None,
            bio_links=getattr(follower, "bio_links", None) if follower else None,
            last_contacted_at=now,
            first_seen_at=now,
            contacted_by_campaign_ids=json.dumps([campaign_id]),
            contact_history=json.dumps([history_entry]),
            scrape_sources="[]",
        )
        db.add(contact)
    await db.commit()
