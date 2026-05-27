"""
Instagram follower scraper using instagrapi.

Flow:
1. Login to Instagram using an available account
2. Resolve target username → user_id
3. Paginate through followers, fetching basic info
4. For each follower batch, fetch detailed bio via user_info()
5. Store followers in DB with status bio_scraped or skipped

Anti-detection:
- Random delays between API calls
- Saves session data to avoid repeated logins
- Switches account on RateLimitError
"""
import asyncio
import json
import random
from datetime import datetime, timedelta
from loguru import logger

from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount, AccountStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.models.activity_log import ActivityLog
from app.utils.crypto import decrypt
from app.utils.exceptions import (
    BotHaltedError, ScraperError, TargetPrivateError, RateLimitError, AccountChallengeError, SoftBlockError
)
from app.utils.instagrapi_client import (
    login as _login,
    acquire_scraping_slot,
    release_scraping_slot,
    get_scraping_account_ids,
)
from app.services.bot_state_service import is_halted


async def scrape_followers(campaign_id: str) -> None:
    """
    Main entry point. Called by the ARQ worker.
    Scrapes all followers of the campaign's target username and stores them in DB.
    """
    async with AsyncSessionLocal() as db:
        from sqlalchemy import select
        result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
        campaign = result.scalar_one_or_none()
        if not campaign:
            logger.error(f"Campaign {campaign_id} not found")
            return

        # Guard against ARQ retrying a job after worker restart when the campaign
        # was already paused/stopped. Without this check, a stale retry would attempt
        # login (possibly failing) and land in except Exception → campaign.status = error.
        _SCRAPING_STATES = (CampaignStatus.scraping, CampaignStatus.scraping_and_running, CampaignStatus.scraping_break)
        if campaign.status not in _SCRAPING_STATES:
            logger.info(
                f"[Scraper] Campaign '{campaign.name}' status='{campaign.status.value}' "
                "at task start — not a scraping state, skipping stale ARQ retry."
            )
            return

        if await is_halted(db):
            logger.warning(f"[Scraper] Global BOT_HALTED at task start - skipping campaign {campaign_id}")
            from app.utils.events import emit as emit_event
            emit_event(campaign_id, "scrape_stopped", "Bot in pausa globale - scraping non avviato", level="warn")
            return

        scrape_mode = getattr(campaign, 'scrape_mode', 'followers')
        mode_label = "following" if scrape_mode == "following" else "follower"
        logger.info(f"Starting scrape for campaign '{campaign.name}' → @{campaign.target_username} (mode: {scrape_mode})")
        from app.utils.events import emit as emit_event
        emit_event(campaign_id, "scrape_start", f"Scraping avviato per @{campaign.target_username} (modalità: {mode_label})")

        _scraping_account_id = None
        try:
            account = await _get_available_account(db, campaign_id=campaign_id)
            if not await acquire_scraping_slot(account.id):
                logger.warning(f"[Scraper] Slot @{account.username} già occupato (TOCTOU) — condiviso")
            _scraping_account_id = account.id
            client = await _login(account, db)
            emit_event(campaign_id, "scrape_start", f"Account @{account.username} connesso, inizio raccolta {mode_label}...")

            # Resolve target user via private API (avoids public endpoint 429)
            # user_info_by_username_v1 uses /api/v1/users/lookup/ (authenticated),
            # unlike user_info_by_username which first tries the public GQL endpoint.
            target_user = None
            for attempt in range(3):
                try:
                    target_user = await asyncio.to_thread(
                        client.user_info_by_username_v1, campaign.target_username
                    )
                    break
                except Exception as e:
                    if ("429" in str(e) or "too many" in str(e).lower()) and attempt < 2:
                        wait = 90 * (attempt + 1)
                        logger.warning(
                            f"Rate limit su user_info_by_username_v1 (tentativo {attempt + 1}/3). "
                            f"Attendo {wait}s..."
                        )
                        await asyncio.sleep(wait)
                    else:
                        raise

            if target_user is None:
                raise ScraperError(f"Impossibile trovare @{campaign.target_username} dopo 3 tentativi")

            if target_user.is_private:
                raise TargetPrivateError(f"@{campaign.target_username} is a private account")

            campaign.target_user_id = target_user.pk
            await db.commit()

            logger.info(f"Target @{campaign.target_username} has pk={target_user.pk}")

            # Scrape followers/following in batches
            total_scraped, scrape_outcome = await _scrape_paginated(client, campaign, account, db, scrape_mode)

            # Refresh: user may have paused/stopped during scraping
            await db.refresh(campaign)

            # Always use actual DB count (handles re-scrape after reset, where
            # many followers already exist from the previous run)
            from sqlalchemy import func as sa_func
            actual_count = await db.scalar(
                select(sa_func.count(Follower.id)).where(Follower.campaign_id == campaign_id)
            ) or 0

            _SCRAPING_STATES = (CampaignStatus.scraping, CampaignStatus.scraping_and_running, CampaignStatus.scraping_break)
            if campaign.status not in _SCRAPING_STATES:
                # User interrupted — preserve their status, just update the count
                campaign.total_followers = actual_count
                campaign.scrape_outcome = "partial"
                campaign.updated_at = datetime.utcnow()
                await db.commit()
                logger.info(
                    f"Scraping interrupted by user at {total_scraped} new followers "
                    f"({actual_count} total). Campaign left in '{campaign.status.value}' status."
                )
                return

            if scrape_outcome == "rate_limited":
                campaign.status = CampaignStatus.paused
                campaign.total_followers = actual_count
                campaign.scrape_outcome = "rate_limited"
                campaign.updated_at = datetime.utcnow()
                await db.commit()
                emit_event(
                    campaign_id,
                    "scrape_stopped",
                    "Scraping interrotto da rate limit ripetuti — ripristinabile (cursore salvato)",
                    level="error",
                )
                logger.warning(
                    f"Scrape rate-limited: {total_scraped} new followers stored "
                    f"({actual_count} total). Campaign paused with cursor saved."
                )
                return

            # Determine final status: if DM workers are already running, transition to running
            if campaign.status == CampaignStatus.scraping_and_running:
                campaign.status = CampaignStatus.running
            else:
                campaign.status = CampaignStatus.ready
            campaign.total_followers = actual_count
            campaign.messages_pending = actual_count
            campaign.scrape_cursor = None
            campaign.scrape_outcome = "completed"
            campaign.scrape_completed_at = datetime.utcnow()
            campaign.updated_at = datetime.utcnow()
            emit_event(campaign_id, "scrape_complete", f"Scraping completato: {actual_count} {mode_label} raccolti. Campagna pronta.")

            log = ActivityLog(campaign_id=campaign.id, action="scrape_completed",
                              details=json.dumps({"total": total_scraped}))
            db.add(log)
            await db.commit()

            logger.info(f"Scrape completed: {total_scraped} followers stored for campaign {campaign_id}")

        except BotHaltedError:
            logger.warning(f"[Scraper] Global BOT_HALTED - saved partial scrape for campaign {campaign_id}")
            from sqlalchemy import func as sa_func
            campaign.total_followers = await db.scalar(
                select(sa_func.count(Follower.id)).where(Follower.campaign_id == campaign_id)
            ) or 0
            campaign.scrape_outcome = "partial"
            campaign.updated_at = datetime.utcnow()
            await db.commit()
            emit_event(campaign_id, "scrape_stopped", "Bot in pausa globale - scraping salvato e interrotto", level="warn")

        except SoftBlockError as e:
            logger.error(f"Scrape soft-blocked for campaign {campaign_id}: {e}")
            campaign.status = CampaignStatus.paused
            campaign.updated_at = datetime.utcnow()
            await db.commit()
            emit_event(campaign_id, "scrape_stopped", "Soft block Instagram rilevato - scraping in pausa per proteggere l'account", level="error")

        except TargetPrivateError as e:
            logger.error(f"Scrape failed: {e}")
            campaign.status = CampaignStatus.error
            await db.commit()

        except Exception as e:
            logger.error(f"Scrape failed for campaign {campaign_id}: {e}")
            campaign.status = CampaignStatus.error
            await db.commit()

        finally:
            if _scraping_account_id:
                await release_scraping_slot(_scraping_account_id)


async def _get_available_account(db, campaign_id: str | None = None) -> InstagramAccount:
    from sqlalchemy import select, func
    from app.models.campaign_account import CampaignAccount

    query = select(InstagramAccount).where(InstagramAccount.status == AccountStatus.active)
    if campaign_id:
        # Only use accounts assigned to this campaign with role scraping or both
        eligible_sq = select(CampaignAccount.account_id).where(
            CampaignAccount.campaign_id == campaign_id,
            CampaignAccount.is_active == True,
            CampaignAccount.role.in_(("scraping", "both")),
        )
        query = query.where(InstagramAccount.id.in_(eligible_sq))

    result = await db.execute(query)
    accounts = result.scalars().all()
    if not accounts:
        raise ScraperError(
            "Nessun account con ruolo 'scraping' o 'both' assegnato a questa campagna. "
            "Assegna un account con ruolo 'scraping' o 'both' dalla sezione account della campagna."
        )
    busy = get_scraping_account_ids()
    free = [a for a in accounts if a.id not in busy]
    if free:
        return random.choice(free)
    logger.warning(
        "[Scraper] Tutti gli account attivi sono già occupati in scraping. "
        "Due campagne usano lo stesso account — rischio 429 aumentato."
    )
    return random.choice(accounts)


async def _get_fallback_account(db, exclude_id: str, campaign_id: str | None = None) -> InstagramAccount | None:
    """Return random active scraping-eligible account excluding exclude_id, or None if unavailable."""
    from sqlalchemy import select
    from app.models.campaign_account import CampaignAccount

    query = select(InstagramAccount).where(
        InstagramAccount.status == AccountStatus.active,
        InstagramAccount.id != exclude_id,
    )
    if campaign_id:
        eligible_sq = select(CampaignAccount.account_id).where(
            CampaignAccount.campaign_id == campaign_id,
            CampaignAccount.is_active == True,
            CampaignAccount.role.in_(("scraping", "both")),
        )
        query = query.where(InstagramAccount.id.in_(eligible_sq))

    result = await db.execute(query)
    accounts = result.scalars().all()
    return random.choice(accounts) if accounts else None


async def _scrape_paginated(client, campaign: Campaign, account: InstagramAccount, db, scrape_mode: str = 'followers') -> tuple[int, str]:
    """Scrape all followers/following with pagination, storing each batch."""
    from instagrapi.exceptions import UserNotFound, LoginRequired
    from sqlalchemy import select

    mode_label = "following" if scrape_mode == "following" else "follower"
    total = 0
    initial_total = await db.scalar(
        select(func.count(Follower.id)).where(Follower.campaign_id == campaign.id)
    ) or 0
    since_last_break = 0
    batch_size = 50
    max_id = getattr(campaign, "scrape_cursor", None) or None
    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 3

    while True:
        try:
            # Global kill-switch — abort scrape immediately if halted.
            if await is_halted(db):
                logger.warning(f"[Scraper] Global BOT_HALTED — aborting scrape for {campaign.id}")
                from app.utils.events import emit as emit_event
                emit_event(campaign.id, "scrape_stopped", "Bot in pausa globale — scraping interrotto", level="warn")
                raise BotHaltedError("global kill-switch active")

            # BUG-NEW-02: check if user paused/stopped the campaign before each batch
            await db.refresh(campaign)
            _SCRAPING_STATES = (CampaignStatus.scraping, CampaignStatus.scraping_and_running, CampaignStatus.scraping_break)
            if campaign.status not in _SCRAPING_STATES:
                logger.info(
                    f"[Scraper] Campaign status changed to '{campaign.status.value}' — "
                    f"scraping interrupted. Saved {total} {mode_label} so far."
                )
                return total, "partial"

            # Small random delay between pagination calls (5-15 sec)
            delay = random.uniform(5, 15)
            await asyncio.sleep(delay)

            # Fetch a batch of followers/following
            followers_batch, max_id = await asyncio.to_thread(
                _fetch_followers_chunk, client, campaign.target_user_id, batch_size, max_id, scrape_mode
            )

            if not followers_batch:
                logger.info(f"No more {mode_label} to scrape (total: {total})")
                break

            consecutive_errors = 0  # Reset on success
            logger.info(
                f"Fetched {len(followers_batch)} {mode_label} "
                f"(total so far: {initial_total + total + len(followers_batch)})"
            )
            from app.utils.events import emit as emit_event
            emit_event(
                campaign.id,
                "scrape_batch",
                f"Scrappati {len(followers_batch)} {mode_label} "
                f"(totale: {initial_total + total + len(followers_batch)})",
            )

            # Store this batch — client/account may rotate on 429
            batch_total, client, account = await _store_followers_batch(
                followers_batch, campaign, db, client, account, scrape_mode
            )
            total += batch_total
            since_last_break += batch_total

            # Update campaign progress in real-time
            campaign.total_followers = initial_total + total
            campaign.scrape_cursor = max_id
            campaign.updated_at = datetime.utcnow()

            # Save session for whichever account is currently active
            account.session_data = json.dumps(client.get_settings())
            account.last_activity_at = datetime.utcnow()
            await db.commit()

            # If max_id is None, we've reached the end
            if not max_id:
                break

            # Configurable session break every N followers
            session_size = getattr(campaign, 'scrape_session_size', 250)
            if since_last_break >= session_size:
                break_min = getattr(campaign, 'scrape_break_minutes_min', 30)
                break_max = getattr(campaign, 'scrape_break_minutes_max', 45)
                minutes = random.uniform(break_min, break_max)
                wake_at = datetime.utcnow() + timedelta(minutes=minutes)
                prev_status = campaign.status.value
                campaign.scrape_break_prev_status = prev_status
                campaign.status = CampaignStatus.scraping_break
                campaign.scrape_break_until = wake_at
                campaign.updated_at = datetime.utcnow()
                await db.commit()
                from app.utils.events import emit as emit_event
                emit_event(campaign.id, "scrape_break",
                    f"Pausa sessione scraping ({int(minutes)} min) dopo {total} profili")
                logger.info(f"[Scraper] Session break: {int(minutes)}min after {total} followers")
                # Interruptible sleep: poll DB every 10s for manual resume or stop
                while datetime.utcnow() < wake_at:
                    await asyncio.sleep(10)
                    if await is_halted(db):
                        logger.warning(f"[Scraper] Global BOT_HALTED during scrape break for {campaign.id}")
                        raise BotHaltedError("global kill-switch active")
                    await db.refresh(campaign)
                    if campaign.status != CampaignStatus.scraping_break:
                        break  # Manual resume or stop
                # Auto-resume if still in scraping_break
                if campaign.status == CampaignStatus.scraping_break:
                    campaign.status = CampaignStatus(prev_status)
                    campaign.scrape_break_until = None
                    campaign.scrape_break_prev_status = None
                    await db.commit()
                    emit_event(campaign.id, "scrape_resume", "Pausa terminata, scraping ripreso")
                since_last_break = 0
                # Check if user stopped during break
                _SCRAPING_STATES = (CampaignStatus.scraping, CampaignStatus.scraping_and_running, CampaignStatus.scraping_break)
                await db.refresh(campaign)
                if campaign.status not in _SCRAPING_STATES:
                    logger.info(f"[Scraper] Campaign stopped during break — saved {total}")
                    return total, "partial"

        except SoftBlockError as e:
            logger.error(f"[Scraper] {e} — scraping interrotto per proteggere l'account.")
            raise

        except Exception as e:
            consecutive_errors += 1
            error_str = str(e).lower()

            # ── Handle 429 Rate Limit ──
            if "429" in error_str or "too many" in error_str or "rate" in error_str:
                if consecutive_errors <= MAX_CONSECUTIVE_ERRORS:
                    wait_time = 60 * consecutive_errors  # 60s, 120s, 180s
                    logger.warning(
                        f"Rate limit 429 durante scraping (tentativo {consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}). "
                        f"Attendo {wait_time}s prima di riprovare..."
                    )
                    await asyncio.sleep(wait_time)
                    continue  # Retry
                else:
                    logger.error(
                        f"Troppe risposte 429 consecutive ({consecutive_errors}). "
                        f"Salvati {total} follower finora. Scraping interrotto."
                    )
                    return total, "rate_limited"

            # ── Other errors ──
            logger.error(f"Errore durante scraping a total={total}: {type(e).__name__}: {e}")
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                logger.error(f"Troppi errori consecutivi ({consecutive_errors}). Interrompo.")
                return total, "partial"

            # Wait a bit and retry once for transient errors
            await asyncio.sleep(random.uniform(10, 30))

    return total, "completed"


def _fetch_followers_chunk(client, user_id: int, amount: int, max_id: str | None, scrape_mode: str = 'followers'):
    """
    Synchronous function (run in thread) to fetch one page of followers or following.
    Returns (users_list, next_max_id).
    """
    if scrape_mode == 'following':
        try:
            result = client.user_following_v1_chunk(user_id, max_id=max_id)
            users, next_cursor = result
            return users, next_cursor or None
        except Exception:
            following_dict = client.user_following(user_id, amount=amount)
            users = list(following_dict.values())
            return users, None
    else:
        try:
            result = client.user_followers_v1_chunk(user_id, max_id=max_id)
            users, next_cursor = result
            return users, next_cursor or None
        except Exception:
            followers_dict = client.user_followers(user_id, amount=amount)
            users = list(followers_dict.values())
            return users, None


async def _store_followers_batch(
    followers_batch, campaign: Campaign, db, client, account: InstagramAccount,
    scrape_mode: str = 'followers',
) -> tuple[int, object, InstagramAccount]:
    """
    Store a batch of followers/following in DB, fetching detailed bio for each.

    S1: if user_info() hits 429, rotates to a fallback account and retries once.
    S4: lognormal delay between user_info() calls.
      - followers mode: 3-8s  (median ~4s)
      - following mode: 8-18s (median ~11s) — business accounts are more monitored

    Returns (stored_count, active_client, active_account) — may differ from input
    if a 429 forced an account rotation mid-batch.
    """
    from sqlalchemy import select

    stored = 0
    current_client = client
    current_account = account
    consecutive_soft_blocks = 0

    for user_short in followers_batch:
        if await is_halted(db):
            logger.warning(f"[Scraper] Global BOT_HALTED mid-batch - stopping after {stored} profiles")
            raise BotHaltedError("global kill-switch active")

        # Check campaign status before each profile — lets pause/stop take effect
        # within one profile's processing time (8-18s) instead of one full batch (15min).
        await db.refresh(campaign)
        _SCRAPING_STATES = (CampaignStatus.scraping, CampaignStatus.scraping_and_running)
        if campaign.status not in _SCRAPING_STATES:
            logger.info(
                f"[Scraper] Campaign status='{campaign.status.value}' detected mid-batch "
                f"after {stored} profiles — stopping immediately."
            )
            return stored, current_client, current_account

        # Check for duplicate
        existing = await db.execute(
            select(Follower).where(
                Follower.campaign_id == campaign.id,
                Follower.ig_user_id == user_short.pk,
            )
        )
        if existing.scalar_one_or_none():
            continue

        # Fetch full user info — S1: rotate account on 429
        biography = None
        is_verified = False
        follower_count = None
        following_count = None
        external_url = None

        for attempt in range(2):
            try:
                # user_info_v1 uses only the authenticated private API (/api/v1/users/{pk}/info/)
                # Avoids the GQL public fallback that user_info() would trigger on 429,
                # which doubles the API call count and hits a separate rate limit.
                user_info = await asyncio.to_thread(current_client.user_info_v1, user_short.pk)
                biography = user_info.biography or None
                is_verified = getattr(user_info, 'is_verified', False) or False
                follower_count = getattr(user_info, 'follower_count', None)
                following_count = getattr(user_info, 'following_count', None)
                ext = getattr(user_info, 'external_url', None)
                external_url = str(ext) if ext else None
                consecutive_soft_blocks = 0
                break
            except Exception as e:
                error_str = str(e).lower()
                is_rate_limit = "429" in error_str or "too many" in error_str or "rate" in error_str
                is_soft_block = "protect" in error_str or "restrict" in error_str or "community" in error_str
                if (is_rate_limit or is_soft_block) and attempt == 0:
                    fallback = await _get_fallback_account(db, exclude_id=current_account.id, campaign_id=campaign.id)
                    kind = "Soft block" if is_soft_block else "429"
                    if fallback:
                        logger.warning(
                            f"[Scraper] {kind} su user_info @{user_short.username}. "
                            f"Rotazione: @{current_account.username} → @{fallback.username}"
                        )
                        try:
                            current_client = await _login(fallback, db)
                            current_account = fallback
                            await asyncio.sleep(random.uniform(30 if is_soft_block else 15, 60 if is_soft_block else 30))
                        except Exception as login_err:
                            logger.warning(f"[Scraper] Fallback login fallito: {login_err}")
                    else:
                        wait = random.uniform(120, 240) if is_soft_block else 60
                        logger.warning(
                            f"[Scraper] {kind} su user_info @{user_short.username}, "
                            f"nessun account alternativo. Attendo {int(wait)}s..."
                        )
                        await asyncio.sleep(wait)
                else:
                    # attempt 1 also failed — store without bio, continue
                    if is_rate_limit or is_soft_block:
                        consecutive_soft_blocks += 1
                        kind = "Soft block" if is_soft_block else "429"
                        logger.warning(
                            f"[Scraper] {kind} persistente su @{user_short.username} dopo retry. "
                            f"Profilo salvato senza bio ({consecutive_soft_blocks} consecutivi)."
                        )
                        await asyncio.sleep(random.uniform(90 if is_soft_block else 30, 180 if is_soft_block else 60))
                    else:
                        logger.warning(f"Could not fetch bio for @{user_short.username}: {e}")
                    break

        if consecutive_soft_blocks >= 3:
            raise SoftBlockError(
                f"3 soft block consecutivi — Instagram blocca attivamente la bio fetch. "
                f"Interruzione per proteggere @{current_account.username}."
            )

        follower = Follower(
            campaign_id=campaign.id,
            ig_user_id=user_short.pk,
            username=user_short.username,
            full_name=user_short.full_name,
            biography=biography,
            is_private=user_short.is_private,
            is_verified=is_verified,
            follower_count=follower_count,
            following_count=following_count,
            external_url=external_url,
            profile_pic_url=str(user_short.profile_pic_url) if user_short.profile_pic_url else None,
            status=FollowerStatus.bio_scraped,
        )
        db.add(follower)
        # Commit per follower — keeps write lock window to milliseconds
        await db.commit()
        stored += 1

        # S4: configurable delay between bio fetches (per-campaign settings)
        delay_min = getattr(campaign, 'bio_fetch_delay_min', 5.0) or 5.0
        delay_max = getattr(campaign, 'bio_fetch_delay_max', 8.0) or 8.0
        delay = random.uniform(delay_min, delay_max)
        await asyncio.sleep(delay)

    return stored, current_client, current_account
