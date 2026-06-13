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
from app.utils.contact_extract import extract_contacts, ContactData
from app.services.global_contact_service import upsert_lead
from app.services.scraping_pool import ScrapingPool, ScrapingPoolEmpty
from app.services.account_manager import has_scrape_budget, increment_scrape_lookup
from app.utils.exceptions import ScrapeBudgetError


def is_challenge_exception(exc: Exception) -> bool:
    """instagrapi usa nomi classe che contengono 'Challenge' per checkpoint/2FA."""
    return "Challenge" in type(exc).__name__


async def isolate_challenged_account(db, campaign, account, exc: Exception) -> None:
    """Isola l'account challenged e mette la campagna in pausa (riprendibile).

    Usata da Fase Lista e Fase Bio: una challenge IG NON deve lasciare l'account
    'active' (ogni retry rifallirebbe) ne' mandare la campagna in 'error' secco.
    """
    from sqlalchemy import select
    from app.models.account import InstagramAccount, AccountStatus
    from app.models.activity_log import ActivityLog
    from app.utils.events import emit as emit_event

    exc_name = type(exc).__name__
    acc = None
    if account is not None:
        acc = (await db.execute(
            select(InstagramAccount).where(InstagramAccount.id == account.id)
        )).scalar_one_or_none()
    acc_label = acc.username if acc else "?"
    if acc:
        acc.status = AccountStatus.challenge_required
    campaign.status = CampaignStatus.paused
    campaign.scrape_outcome = "challenge"
    campaign.updated_at = datetime.utcnow()
    db.add(ActivityLog(
        campaign_id=campaign.id,
        action="challenge",
        details=json.dumps({"account": acc_label, "exc": exc_name}),
    ))
    await db.commit()
    logger.error(f"[Scraper] Challenge IG su @{acc_label} ({exc_name}) — account isolato, campagna in pausa")
    emit_event(
        campaign.id, "scrape_stopped",
        f"Instagram richiede verifica su @{acc_label}. Risolvi la challenge (app/web IG), poi ri-login browser e riavvia.",
        level="error",
    )


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

        pool = None
        try:
            pool = await ScrapingPool.build(db, campaign)
            sel = pool.next(campaign)
            if sel is None:
                raise ScrapeBudgetError("Cap raggiunto su tutti gli account scraping all'avvio")
            account, client = sel  # usato per la risoluzione target
            emit_event(
                campaign_id, "scrape_start",
                f"{pool.size} account scraping connessi, inizio raccolta {mode_label}...",
            )

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

            campaign.target_user_id = int(target_user.pk)  # pk e' str in instagrapi; colonna BIGINT
            await db.commit()

            logger.info(f"Target @{campaign.target_username} has pk={target_user.pk}")

            # Scrape followers/following in batches
            total_scraped, scrape_outcome = await _scrape_paginated(pool, campaign, db, scrape_mode)

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

            if scrape_outcome in ("rate_limited", "scrape_capped"):
                campaign.status = CampaignStatus.paused
                campaign.total_followers = actual_count
                campaign.scrape_outcome = scrape_outcome
                campaign.updated_at = datetime.utcnow()
                await db.commit()
                msg = ("Scraping in pausa: cap lookup giornaliero raggiunto — riprende dopo il reset"
                       if scrape_outcome == "scrape_capped"
                       else "Scraping interrotto da rate limit ripetuti — ripristinabile (cursore salvato)")
                emit_event(
                    campaign_id,
                    "scrape_stopped",
                    msg,
                    level="warn" if scrape_outcome == "scrape_capped" else "error",
                )
                logger.warning(
                    f"Scrape rate-limited: {total_scraped} new followers stored "
                    f"({actual_count} total). Campaign paused with cursor saved."
                )
                return

            # Determine final status: if DM workers are already running, transition to running
            if campaign.status == CampaignStatus.scraping_and_running:
                campaign.status = CampaignStatus.running
            elif not campaign.messaging_enabled:
                campaign.status = CampaignStatus.completed
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

        except ScrapingPoolEmpty as e:
            logger.error(f"Scrape non avviato: {e}")
            campaign.status = CampaignStatus.error
            await db.commit()
            emit_event(campaign_id, "scrape_stopped", f"Scraping non avviato: {e}", level="error")

        except Exception as e:
            if is_challenge_exception(e) and locals().get("account") is not None:
                await isolate_challenged_account(db, campaign, locals().get("account"), e)
            else:
                logger.error(f"Scrape failed for campaign {campaign_id}: {e}")
                campaign.status = CampaignStatus.error
                await db.commit()

        finally:
            if pool is not None:
                try:
                    await pool.save_sessions(db)
                except Exception as exc:
                    logger.warning(f"[Scraper] save_sessions finale fallito: {exc}")
                await pool.release()


async def fetch_and_store_bio(follower, campaign, db, pool):
    """Estrae bio+contatti per UN follower gia' in DB (status pending) e lo porta
    a bio_scraped.

    Ritorna una tupla ``(outcome, account_used, error)``:
      - outcome: 'done' | 'soft_block' | 'capped' | 'challenge' | 'network' | 'error'
      - account_used: l'account che ha eseguito la chiamata IG (None se 'capped')
      - error: l'eccezione catturata su 'challenge'/'error' (None altrimenti)

    Il chiamante riceve cosi' l'account REALE usato per la chiamata: indispensabile
    per isolare l'account giusto su challenge (con la rotazione pool round-robin il
    chiamante non puo' indovinare quale account ha fatto la lookup).
    Riusa rotazione pool / cap / extract_contacts come _store_followers_batch.
    """
    sel = pool.next(campaign)
    if sel is None:
        return "capped", None, None
    current_account, current_client = sel
    try:
        user_info = await asyncio.to_thread(current_client.user_info_v1, follower.ig_user_id)
    except Exception as e:
        if is_challenge_exception(e):
            return "challenge", current_account, e
        es = str(e).lower()
        if "protect" in es or "restrict" in es or "community" in es:
            return "soft_block", current_account, None
        if "429" in es or "too many" in es or "rate" in es:
            return "soft_block", current_account, None
        # Connessione caduta (tethering USB staccato, proxy giu', DNS): NON e' colpa
        # del profilo. Il chiamante mette in pausa la run preservando i pending,
        # invece di skippare profili buoni o ciclare a vuoto.
        if isinstance(e, (ConnectionError, TimeoutError, OSError)) or any(
            k in es for k in (
                "connection", "timed out", "timeout", "proxy", "max retries",
                "network", "unreachable", "reset by peer", "getaddrinfo",
                "resolve", "ssl", "tunnel", "aborted",
            )
        ):
            logger.warning(f"[Bio] user_info @{follower.username} errore di rete: {e}")
            return "network", current_account, e
        # Errore di parsing/dati specifico del profilo (es. KeyError
        # 'pinned_channels_info' quando IG cambia schema): il chiamante skippa
        # questo profilo e avanza.
        logger.warning(f"[Bio] user_info @{follower.username} fallito: {e}")
        return "error", current_account, e

    from app.utils.contact_extract import extract_contacts
    contacts = extract_contacts(user_info)
    # Una sola sorgente per il contatore cap: bump in-memory, persistito dal
    # db.commit() sotto. NON usare anche increment_scrape_lookup (UPDATE atomico
    # che committa subito): con expire_on_commit=False i due incrementi si
    # sommano -> doppio conteggio (cap raggiunto a meta'). Il bump in-memory e'
    # gia' visibile a pool.next nello stesso run.
    from app.services.account_manager import bump_scrape_lookup
    bump_scrape_lookup(current_account)

    follower.biography = user_info.biography or None
    follower.is_verified = getattr(user_info, "is_verified", False) or False
    follower.follower_count = getattr(user_info, "follower_count", None)
    follower.following_count = getattr(user_info, "following_count", None)
    ext = getattr(user_info, "external_url", None)
    follower.external_url = contacts.external_url or (str(ext) if ext else None)
    follower.phone = contacts.phone
    follower.email = contacts.email
    follower.whatsapp = contacts.whatsapp
    follower.bio_links = json.dumps(contacts.bio_links) if contacts.bio_links else None
    follower.contact_source = json.dumps(contacts.sources) if contacts.sources else None
    follower.status = FollowerStatus.bio_scraped
    await db.commit()

    await upsert_lead(
        db,
        ig_user_id=follower.ig_user_id,
        username=follower.username,
        full_name=follower.full_name,
        biography=follower.biography,
        contacts=contacts,
        campaign=campaign,
        account=current_account,
    )

    logger.info(
        f"[Bio] @{follower.username} via @{current_account.username} "
        f"(lookups oggi: {current_account.scrape_lookups_today})"
    )
    return "done", current_account, None


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
    busy = get_scraping_account_ids()
    free = [a for a in accounts if a.id not in busy]
    return random.choice(free or accounts) if accounts else None


async def _eligible_scraping_accounts(db, campaign_id: str) -> list[InstagramAccount]:
    """Tutti gli account attivi con ruolo scraping/both assegnati alla campagna."""
    from sqlalchemy import select
    from app.models.campaign_account import CampaignAccount

    eligible_sq = select(CampaignAccount.account_id).where(
        CampaignAccount.campaign_id == campaign_id,
        CampaignAccount.is_active == True,
        CampaignAccount.role.in_(("scraping", "both")),
    )
    result = await db.execute(
        select(InstagramAccount).where(
            InstagramAccount.status == AccountStatus.active,
            InstagramAccount.id.in_(eligible_sq),
        )
    )
    return list(result.scalars().all())


async def _scrape_paginated(pool, campaign: Campaign, db, scrape_mode: str = 'followers') -> tuple[int, str]:
    """Scrape all followers/following with pagination, storing each batch."""
    from instagrapi.exceptions import UserNotFound, LoginRequired
    from sqlalchemy import select

    # La paginazione lista resta su UN account del pool (chiamate cheap, non vanno ruotate).
    list_sel = pool.next(campaign)
    if list_sel is None:
        raise ScrapeBudgetError("Cap raggiunto su tutti gli account scraping (paginazione)")
    list_account, list_client = list_sel

    mode_label = "following" if scrape_mode == "following" else "follower"
    total = 0
    initial_total = await db.scalar(
        select(func.count(Follower.id)).where(Follower.campaign_id == campaign.id)
    ) or 0
    since_last_break = 0
    from app.config import settings as _scfg
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

            # Human-like delay between follower-LIST pagination calls.
            # This endpoint is the #1 IG bot-detection target — slow + irregular.
            # Uniform draw in the configured range is sufficient at 5-10 s; the
            # lognormal complexity bought nothing and clamped ~46% of samples to lo.
            # An occasional long "distraction" pause is kept for realism.
            from app.config import settings as _s
            if random.random() < _s.list_long_pause_probability:
                delay = random.uniform(
                    _s.list_long_pause_min_seconds,
                    _s.list_long_pause_max_seconds,
                )
                logger.info(f"[Scraper] Pausa lunga {delay:.0f}s tra pagine (simulazione distrazione umana)")
            else:
                # Uniform in [list_page_delay_min_seconds, list_page_delay_max_seconds]
                delay = random.uniform(_s.list_page_delay_min_seconds, _s.list_page_delay_max_seconds)
            await asyncio.sleep(delay)

            # Fetch a batch of followers/following (batch_size re-randomized per page)
            batch_size = random.randint(_scfg.list_page_size_min, _scfg.list_page_size_max)
            followers_batch, max_id = await asyncio.to_thread(
                _fetch_followers_chunk, list_client, campaign.target_user_id, batch_size, max_id, scrape_mode
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

            # Store this batch — ogni lead usa il prossimo account del pool (round-robin)
            batch_total = await _store_followers_batch(
                followers_batch, campaign, db, pool, scrape_mode,
            )
            total += batch_total
            since_last_break += batch_total

            # Salva le sessioni di tutti gli account del pool (commit incluso)
            campaign.total_followers = initial_total + total
            campaign.scrape_cursor = max_id
            campaign.updated_at = datetime.utcnow()
            await pool.save_sessions(db)

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

        except ScrapeBudgetError as e:
            logger.warning(f"[Scraper] {e} — scraping in pausa fino al reset giornaliero.")
            return total, "scrape_capped"

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
    # CRITICO: passare max_amount=amount. Senza, instagrapi fa un loop interno
    # che drena l'intera lista in un burst di richieste count=200 senza delay →
    # challenge IG immediata. Con max_amount la chiamata ritorna ~amount utenti
    # e poi il delay tra pagine in _scrape_paginated agisce (scroll umano).
    if scrape_mode == 'following':
        try:
            result = client.user_following_v1_chunk(user_id, max_amount=amount, max_id=max_id or "")
            users, next_cursor = result
            return users, next_cursor or None
        except Exception:
            # Mid-paginazione: un errore qui NON significa "lista finita". Il fallback
            # user_following() ignora max_id (riparte dall'inizio) e ritorna cursor=None
            # => verrebbe interpretato come fine lista, azzerando il cursore su un semplice
            # throttle/429. Ri-solleva cosi' il caller lo gestisce (retry/challenge) e il
            # cursore resta intatto. Il fallback resta valido SOLO alla prima pagina.
            if max_id:
                raise
            following_dict = client.user_following(user_id, amount=amount)
            users = list(following_dict.values())
            return users, None
    else:
        try:
            result = client.user_followers_v1_chunk(user_id, max_amount=amount, max_id=max_id or "")
            users, next_cursor = result
            return users, next_cursor or None
        except Exception:
            # Vedi nota sopra (branch following): non mascherare un throttle mid-paginazione
            # come fine lista. Fallback non-chunk solo alla prima pagina.
            if max_id:
                raise
            followers_dict = client.user_followers(user_id, amount=amount)
            users = list(followers_dict.values())
            return users, None


async def _store_followers_batch(
    followers_batch, campaign: Campaign, db, pool, scrape_mode: str = 'followers',
) -> int:
    """
    Store a batch of followers/following in DB, fetching detailed bio for each.

    Approccio C: ogni lead usa il prossimo account del pool (round-robin). Il cap
    per-account è gestito da pool.next (salta gli account a cap; None = tutti a cap).
    Su 429/soft-block si ruota al prossimo account del pool e si riprova una volta.

    Returns stored_count.
    """
    from sqlalchemy import select

    stored = 0
    consecutive_soft_blocks = 0

    for user_short in followers_batch:
        if await is_halted(db):
            logger.warning(f"[Scraper] Global BOT_HALTED mid-batch - stopping after {stored} profiles")
            raise BotHaltedError("global kill-switch active")

        # Check campaign status before each profile — lets pause/stop take effect quickly.
        await db.refresh(campaign)
        _SCRAPING_STATES = (CampaignStatus.scraping, CampaignStatus.scraping_and_running)
        if campaign.status not in _SCRAPING_STATES:
            logger.info(
                f"[Scraper] Campaign status='{campaign.status.value}' detected mid-batch "
                f"after {stored} profiles — stopping immediately."
            )
            return stored

        # Check for duplicate
        existing = await db.execute(
            select(Follower).where(
                Follower.campaign_id == campaign.id,
                Follower.ig_user_id == int(user_short.pk),
            )
        )
        if existing.scalar_one_or_none():
            continue

        # Round-robin: prossimo account con budget. None = tutti a cap.
        sel = pool.next(campaign)
        if sel is None:
            raise ScrapeBudgetError(
                "Cap lookup giornaliero raggiunto su tutti gli account scraping disponibili"
            )
        current_account, current_client = sel

        biography = None
        is_verified = False
        follower_count = None
        following_count = None
        external_url = None
        contacts = ContactData()

        for attempt in range(2):
            try:
                # user_info_v1 usa solo l'API privata autenticata (/api/v1/users/{pk}/info/).
                user_info = await asyncio.to_thread(current_client.user_info_v1, user_short.pk)
                biography = user_info.biography or None
                is_verified = getattr(user_info, 'is_verified', False) or False
                follower_count = getattr(user_info, 'follower_count', None)
                following_count = getattr(user_info, 'following_count', None)
                ext = getattr(user_info, 'external_url', None)
                external_url = str(ext) if ext else None
                contacts = extract_contacts(user_info)
                # Un solo conteggio: bump in-memory date-aware, persistito dal commit
                # del batch. (Prima sommava anche increment_scrape_lookup -> doppio.)
                from app.services.account_manager import bump_scrape_lookup
                bump_scrape_lookup(current_account)
                consecutive_soft_blocks = 0
                break
            except Exception as e:
                error_str = str(e).lower()
                is_rate_limit = "429" in error_str or "too many" in error_str or "rate" in error_str
                is_soft_block = "protect" in error_str or "restrict" in error_str or "community" in error_str
                if (is_rate_limit or is_soft_block) and attempt == 0:
                    kind = "Soft block" if is_soft_block else "429"
                    alt = pool.next(campaign)
                    if alt is not None and alt[0].id != current_account.id:
                        logger.warning(
                            f"[Scraper] {kind} su user_info @{user_short.username}. "
                            f"Rotazione pool: @{current_account.username} → @{alt[0].username}"
                        )
                        current_account, current_client = alt
                        await asyncio.sleep(random.uniform(30 if is_soft_block else 15, 60 if is_soft_block else 30))
                    else:
                        wait = random.uniform(120, 240) if is_soft_block else 60
                        logger.warning(
                            f"[Scraper] {kind} su user_info @{user_short.username}, "
                            f"nessun account alternativo nel pool. Attendo {int(wait)}s..."
                        )
                        await asyncio.sleep(wait)
                else:
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
            ig_user_id=int(user_short.pk),
            username=user_short.username,
            full_name=user_short.full_name,
            biography=biography,
            is_private=user_short.is_private,
            is_verified=is_verified,
            follower_count=follower_count,
            following_count=following_count,
            external_url=contacts.external_url or external_url,
            profile_pic_url=str(user_short.profile_pic_url) if user_short.profile_pic_url else None,
            phone=contacts.phone,
            email=contacts.email,
            whatsapp=contacts.whatsapp,
            bio_links=json.dumps(contacts.bio_links) if contacts.bio_links else None,
            contact_source=json.dumps(contacts.sources) if contacts.sources else None,
            status=FollowerStatus.bio_scraped,
        )
        db.add(follower)
        await db.commit()
        stored += 1

        # Log per-lead con l'account che ha fatto il lookup (visibilita' round-robin).
        logger.info(
            f"[Scraper] @{user_short.username} bio via @{current_account.username} "
            f"(lookups oggi: {current_account.scrape_lookups_today})"
        )

        await upsert_lead(
            db,
            ig_user_id=int(user_short.pk),
            username=user_short.username,
            full_name=user_short.full_name,
            biography=biography,
            contacts=contacts,
            campaign=campaign,
            account=current_account,
        )

        # Delay configurabile tra bio fetch (per-campagna). NB: è GLOBALE per-lead,
        # condiviso tra gli account del pool (vedi helper text UI).
        delay_min = getattr(campaign, 'bio_fetch_delay_min', 5.0) or 5.0
        delay_max = getattr(campaign, 'bio_fetch_delay_max', 8.0) or 8.0
        delay = random.uniform(delay_min, delay_max)
        await asyncio.sleep(delay)

    return stored
