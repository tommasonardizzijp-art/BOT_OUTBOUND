"""Fase Lista: raccoglie solo info base dei follower a blocchetti paced.

NON chiama user_info_v1 (nessun consumo di cap). Crea Follower(status=pending)
che la Fase Bio (scrape_bios.py) processera' poi. Riusa ScrapingPool, il challenge
handler e _fetch_followers_chunk dello scraper esistente.
"""
import asyncio
import random
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import func, select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.services.bot_state_service import is_halted
from app.services.scraping_pool import ScrapingPool, ScrapingPoolEmpty
from app.services.scraper import _fetch_followers_chunk, is_challenge_exception, isolate_challenged_account
from app.utils.exceptions import BotHaltedError, ScrapeBudgetError, ScraperError, TargetPrivateError


def next_page_size() -> int:
    """Dimensione pagina lista randomizzata nei bound di config."""
    return random.randint(settings.list_page_size_min, settings.list_page_size_max)


def remaining_for_target(target: int | None, already: int, page: int) -> int:
    """Quanti follower richiedere in questa pagina dato il target.

    target None = illimitato -> page intero. Altrimenti clamp a (target-already), min 0.
    """
    if target is None:
        return page
    return max(0, min(page, target - already))


async def _list_page_delay() -> None:
    """Delay lognormale tra pagine + pausa lunga occasionale."""
    if random.random() < settings.list_long_pause_probability:
        delay = random.uniform(settings.list_long_pause_min_seconds, settings.list_long_pause_max_seconds)
        logger.info(f"[Lista] Pausa lunga {delay:.0f}s (scroll fermo)")
    else:
        lo, hi = settings.list_page_delay_min_seconds, settings.list_page_delay_max_seconds
        mid = (lo + hi) / 2
        delay = min(hi, max(lo, random.lognormvariate(0, 0.4) * mid))
    await asyncio.sleep(delay)


async def list_followers(campaign_id: str) -> None:
    """Entry point Fase Lista. Chiamata dal worker."""
    async with AsyncSessionLocal() as db:
        campaign = (await db.execute(select(Campaign).where(Campaign.id == campaign_id))).scalar_one_or_none()
        if not campaign:
            logger.error(f"[Lista] Campaign {campaign_id} not found")
            return
        if campaign.status not in (CampaignStatus.listing, CampaignStatus.listing_break):
            logger.info(f"[Lista] Campaign status='{campaign.status.value}' — skip stale retry")
            return
        if await is_halted(db):
            from app.utils.events import emit as emit_event
            emit_event(campaign_id, "scrape_stopped", "Bot in pausa globale — lista non avviata", level="warn")
            return

        scrape_mode = getattr(campaign, "scrape_mode", "followers")
        mode_label = "following" if scrape_mode == "following" else "follower"
        pool = None
        account = None
        try:
            from app.utils.events import emit as emit_event

            pool = await ScrapingPool.build(db, campaign)
            sel = pool.next(campaign)
            if sel is None:
                raise ScrapeBudgetError("Nessun account scraping disponibile")
            account, client = sel

            # Risolvi target se non gia' fatto
            if not campaign.target_user_id:
                target_user = await asyncio.to_thread(client.user_info_by_username_v1, campaign.target_username)
                if target_user.is_private:
                    raise TargetPrivateError(f"@{campaign.target_username} privato")
                campaign.target_user_id = int(target_user.pk)  # pk e' str in instagrapi; colonna BIGINT
                await db.commit()

            emit_event(campaign_id, "scrape_start", f"Fase Lista avviata ({mode_label}) — target {campaign.list_target or 'tutta la lista'}")
            already = await db.scalar(select(func.count(Follower.id)).where(Follower.campaign_id == campaign_id)) or 0
            max_id = campaign.scrape_cursor or None
            since_break = 0

            while True:
                if await is_halted(db):
                    raise BotHaltedError("kill-switch")
                await db.refresh(campaign)
                if campaign.status not in (CampaignStatus.listing, CampaignStatus.listing_break):
                    logger.info(f"[Lista] Stato '{campaign.status.value}' — interrotto a {already}")
                    return
                # Target raggiunto?
                page = remaining_for_target(campaign.list_target, already, next_page_size())
                if page == 0:
                    logger.info(f"[Lista] Target {campaign.list_target} raggiunto ({already})")
                    break

                # Rotazione account per-pagina: ogni pagina la richiede un account
                # diverso del pool (round-robin). Il cursore max_id e' lato-IG e
                # funziona con qualunque account lo presenti, quindi dimezza (con 2
                # account) le richieste di lista per-account = footprint piu' basso.
                # La Fase Lista non consuma cap, quindi pool.next non salta mai per cap.
                sel = pool.next(campaign)
                if sel is None:
                    raise ScrapeBudgetError("Nessun account scraping disponibile")
                account, client = sel

                await _list_page_delay()
                batch, max_id = await asyncio.to_thread(
                    _fetch_followers_chunk, client, campaign.target_user_id, page, max_id, scrape_mode
                )
                logger.info(f"[Lista] pagina via @{account.username}: +{len(batch)} (totale ~{already + len(batch)})")
                if not batch:
                    logger.info(f"[Lista] Lista IG esaurita ({already})")
                    break

                stored = 0
                for us in batch:
                    exists = await db.scalar(
                        select(Follower.id).where(
                            Follower.campaign_id == campaign_id,
                            Follower.ig_user_id == int(us.pk),
                        )
                    )
                    if exists:
                        continue
                    db.add(Follower(
                        campaign_id=campaign_id,
                        ig_user_id=int(us.pk),
                        username=us.username,
                        full_name=us.full_name,
                        is_private=us.is_private,
                        is_verified=getattr(us, "is_verified", False) or False,
                        profile_pic_url=str(us.profile_pic_url) if us.profile_pic_url else None,
                        status=FollowerStatus.pending,
                    ))
                    stored += 1
                already += stored
                since_break += stored
                campaign.scrape_cursor = max_id
                campaign.total_followers = already
                campaign.updated_at = datetime.utcnow()
                await db.commit()
                emit_event(campaign_id, "scrape_batch", f"Lista: {already}" + (f"/{campaign.list_target}" if campaign.list_target else ""))

                if not max_id:
                    logger.info(f"[Lista] Fine lista ({already})")
                    break

                # Pausa sessione lista
                if since_break >= getattr(campaign, "scrape_session_size", 250):
                    minutes = random.uniform(
                        getattr(campaign, "scrape_break_minutes_min", 30),
                        getattr(campaign, "scrape_break_minutes_max", 45),
                    )
                    campaign.scrape_break_prev_status = CampaignStatus.listing.value
                    campaign.status = CampaignStatus.listing_break
                    campaign.scrape_break_until = datetime.utcnow() + timedelta(minutes=minutes)
                    campaign.updated_at = datetime.utcnow()
                    await db.commit()
                    emit_event(campaign_id, "scrape_break", f"Pausa lista {int(minutes)} min dopo {already}")
                    return  # il resume riaccoda il job

            # Completata: torna a ready (o resta listing-done -> ready)
            campaign.status = CampaignStatus.ready
            campaign.scrape_cursor = None
            campaign.updated_at = datetime.utcnow()
            await db.commit()
            emit_event(campaign_id, "scrape_complete", f"Fase Lista completata: {already} follower in lista")

        except BotHaltedError:
            from app.utils.events import emit as emit_event
            campaign.status = CampaignStatus.paused
            campaign.updated_at = datetime.utcnow()
            await db.commit()
            emit_event(campaign_id, "scrape_stopped", "Bot in pausa globale — lista interrotta", level="warn")
        except (ScrapeBudgetError, ScrapingPoolEmpty, TargetPrivateError, ScraperError) as e:
            from app.utils.events import emit as emit_event
            campaign.status = CampaignStatus.error
            await db.commit()
            emit_event(campaign_id, "scrape_stopped", f"Fase Lista non avviata: {e}", level="error")
        except Exception as e:
            if is_challenge_exception(e) and account is not None:
                await isolate_challenged_account(db, campaign, account, e)
            else:
                logger.error(f"[Lista] Errore campaign {campaign_id}: {e}")
                campaign.status = CampaignStatus.error
                await db.commit()
        finally:
            if pool is not None:
                try:
                    await pool.save_sessions(db)
                except Exception as exc:
                    logger.warning(f"[Lista] save_sessions fallito: {exc}")
                await pool.release()
