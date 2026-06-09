"""Fase Bio: estrae bio+contatti dai Follower(status=pending) gia' in lista."""
import asyncio
import random
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import func, select

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.services.bot_state_service import is_halted
from app.services.scraping_pool import ScrapingPool, ScrapingPoolEmpty
from app.services.scraper import fetch_and_store_bio, is_challenge_exception, isolate_challenged_account
from app.utils.exceptions import BotHaltedError, ScrapeBudgetError, SoftBlockError


def bio_should_continue(target: int | None, done: int) -> bool:
    """True se la Fase Bio deve continuare dato il target e i gia' fatti."""
    if target is None:
        return True
    return done < target


async def scrape_bios(campaign_id: str) -> None:
    """Entry point Fase Bio. Chiamata dal worker."""
    async with AsyncSessionLocal() as db:
        campaign = (await db.execute(select(Campaign).where(Campaign.id == campaign_id))).scalar_one_or_none()
        if not campaign:
            return
        if campaign.status not in (CampaignStatus.scraping, CampaignStatus.scraping_break):
            logger.info(f"[Bio] Stato '{campaign.status.value}' — skip stale retry")
            return
        if await is_halted(db):
            from app.utils.events import emit as emit_event
            emit_event(campaign_id, "scrape_stopped", "Bot in pausa globale — bio non avviata", level="warn")
            return

        pool = None
        account = None
        # bio_target e' un TOTALE, non un per-run: seed done con le bio gia' estratte
        # cosi' un resume punta al totale (coerente con bio_progress nella UI) invece
        # di rifare bio_target lookup da capo ad ogni ripresa.
        done = await db.scalar(
            select(func.count(Follower.id)).where(
                Follower.campaign_id == campaign_id,
                Follower.status == FollowerStatus.bio_scraped,
            )
        ) or 0
        consecutive_soft = 0
        try:
            from app.utils.events import emit as emit_event

            pool = await ScrapingPool.build(db, campaign)
            emit_event(campaign_id, "scrape_start", f"Fase Bio avviata — target {campaign.bio_target or 'tutti i pending'}")
            since_break = 0

            while bio_should_continue(campaign.bio_target, done):
                if await is_halted(db):
                    raise BotHaltedError("kill-switch")
                await db.refresh(campaign)
                if campaign.status not in (CampaignStatus.scraping, CampaignStatus.scraping_break):
                    logger.info(f"[Bio] Stato '{campaign.status.value}' — interrotto a {done}")
                    return

                follower = (await db.execute(
                    select(Follower).where(
                        Follower.campaign_id == campaign_id,
                        Follower.status == FollowerStatus.pending,
                    ).limit(1)
                )).scalar_one_or_none()
                if follower is None:
                    logger.info(f"[Bio] Nessun pending rimasto ({done} fatti)")
                    break

                # fetch_and_store_bio ritorna l'account REALE usato per la lookup
                # (rotazione pool interna): serve per isolare quello giusto su challenge.
                outcome, account, err = await fetch_and_store_bio(follower, campaign, db, pool)

                if outcome == "capped":
                    campaign.status = CampaignStatus.paused
                    campaign.scrape_outcome = "scrape_capped"
                    campaign.updated_at = datetime.utcnow()
                    await db.commit()
                    emit_event(campaign_id, "scrape_stopped", "Cap giornaliero raggiunto — riprende dopo reset", level="warn")
                    return

                if outcome == "challenge":
                    await isolate_challenged_account(db, campaign, account, err)
                    return

                if outcome == "soft_block":
                    consecutive_soft += 1
                    if consecutive_soft >= 3:
                        raise SoftBlockError("3 soft block consecutivi")
                    await asyncio.sleep(random.uniform(90, 180))
                    continue

                if outcome == "done":
                    consecutive_soft = 0
                    done += 1
                    since_break += 1
                    delay = random.uniform(
                        getattr(campaign, "bio_fetch_delay_min", 5.0) or 5.0,
                        getattr(campaign, "bio_fetch_delay_max", 8.0) or 8.0,
                    )
                    await asyncio.sleep(delay)

                if since_break >= getattr(campaign, "scrape_session_size", 250):
                    minutes = random.uniform(
                        getattr(campaign, "scrape_break_minutes_min", 30),
                        getattr(campaign, "scrape_break_minutes_max", 45),
                    )
                    campaign.scrape_break_prev_status = CampaignStatus.scraping.value
                    campaign.status = CampaignStatus.scraping_break
                    campaign.scrape_break_until = datetime.utcnow() + timedelta(minutes=minutes)
                    campaign.updated_at = datetime.utcnow()
                    await db.commit()
                    emit_event(campaign_id, "scrape_break", f"Pausa bio {int(minutes)} min dopo {done}")
                    return

            campaign.status = CampaignStatus.ready
            campaign.updated_at = datetime.utcnow()
            await db.commit()
            emit_event(campaign_id, "scrape_complete", f"Fase Bio completata: {done} bio estratte")

        except BotHaltedError:
            from app.utils.events import emit as emit_event
            campaign.status = CampaignStatus.paused
            campaign.updated_at = datetime.utcnow()
            await db.commit()
            emit_event(campaign_id, "scrape_stopped", "Bot in pausa globale — bio interrotta", level="warn")

        except SoftBlockError as e:
            from app.utils.events import emit as emit_event
            campaign.status = CampaignStatus.paused
            campaign.updated_at = datetime.utcnow()
            await db.commit()
            emit_event(campaign_id, "scrape_stopped", f"Soft block — bio in pausa: {e}", level="error")

        except (ScrapeBudgetError, ScrapingPoolEmpty) as e:
            from app.utils.events import emit as emit_event
            campaign.status = CampaignStatus.error
            campaign.updated_at = datetime.utcnow()
            await db.commit()
            emit_event(campaign_id, "scrape_stopped", f"Fase Bio non avviata: {e}", level="error")

        except Exception as e:
            if is_challenge_exception(e) and account is not None:
                await isolate_challenged_account(db, campaign, account, e)
            else:
                logger.error(f"[Bio] Errore {campaign_id}: {e}")
                campaign.status = CampaignStatus.error
                campaign.updated_at = datetime.utcnow()
                await db.commit()

        finally:
            if pool is not None:
                try:
                    await pool.save_sessions(db)
                except Exception as exc:
                    logger.warning(f"[Bio] save_sessions fallito: {exc}")
                await pool.release()
