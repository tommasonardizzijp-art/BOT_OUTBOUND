"""Import resolver: turn imported profile lines into bio-scraped Followers.

Reuses the scraper's account selection + instagrapi login. Resolution itself
uses user_info_by_username_v1 (1 call → pk + full bio).
"""
import asyncio
import json
import random
import uuid
from datetime import datetime, timedelta
from loguru import logger
from sqlalchemy import select
from instagrapi.exceptions import UserNotFound

from app.database import AsyncSessionLocal
from app.models.imported_profile import ImportedProfile
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.models.activity_log import ActivityLog
from app.utils.ig_username import parse_lines
from app.utils.exceptions import BotHaltedError, ScraperError
from app.services.scraping_pool import ScrapingPool, ScrapingPoolEmpty
from app.services.bot_state_service import is_halted
from app.utils.events import emit as emit_event
from app.utils.contact_extract import extract_contacts
from app.services.global_contact_service import upsert_lead
from app.services.account_manager import increment_scrape_lookup


def classify_resolution(user_info, error) -> tuple[str, bool]:
    """Map an IG resolution outcome → (staging_status, should_create_follower).

    - success public  → ('resolved', True)
    - success private → ('private', True)   # Follower comunque creato
    - UserNotFound    → ('not_found', False)
    - other exception → ('error', False)
    """
    if error is not None:
        if isinstance(error, UserNotFound):
            return "not_found", False
        return "error", False
    if getattr(user_info, "is_private", False):
        return "private", True
    return "resolved", True


async def store_imported_lines(db, campaign_id: str, raw: str) -> dict:
    """Parse a file blob and insert pending ImportedProfile rows.
    Returns counts; raises ValueError if zero valid lines."""
    parsed = parse_lines(raw)
    if not parsed["valid"]:
        raise ValueError("Nessun profilo valido trovato nel file.")

    # Dedup contro righe già presenti per questa campagna
    existing = await db.execute(
        select(ImportedProfile.username).where(ImportedProfile.campaign_id == campaign_id)
    )
    existing_usernames = {r[0] for r in existing.all()}

    inserted = 0
    skipped_existing = 0
    for username, raw_line in parsed["valid"]:
        if username in existing_usernames:
            skipped_existing += 1
            continue
        db.add(ImportedProfile(
            id=str(uuid.uuid4()),
            campaign_id=campaign_id,
            raw_input=raw_line[:512],
            username=username,
            status="pending",
        ))
        inserted += 1
    await db.commit()
    logger.info(f"[Import] Campaign {campaign_id}: {inserted} profili inseriti, "
                f"{parsed['duplicates']} duplicati file, {skipped_existing} già presenti, "
                f"{parsed['skipped_invalid']} righe scartate")
    return {
        "inserted": inserted,
        "duplicates_in_file": parsed["duplicates"],
        "skipped_existing": skipped_existing,
        "skipped_invalid": parsed["skipped_invalid"],
    }


async def _resolve_one(db, campaign, username, pool, current_account, current_client):
    """Resolve a single username → (user_info | None, error | None, account_used).

    Approccio C: su 429/soft-block ruota al prossimo account del pool (già loggato,
    niente re-login). Il client per-riga è scelto dal chiamante via pool.next.
    """
    for attempt in range(2):
        try:
            info = await asyncio.to_thread(current_client.user_info_by_username_v1, username)
            return info, None, current_account
        except UserNotFound as e:
            return None, e, current_account
        except Exception as e:
            es = str(e).lower()
            is_rate = "429" in es or "too many" in es or "rate" in es
            is_soft = "protect" in es or "restrict" in es or "community" in es
            if (is_rate or is_soft) and attempt == 0:
                alt = pool.next(campaign)
                if alt is not None and alt[0].id != current_account.id:
                    logger.warning(f"[Import] {'soft-block' if is_soft else '429'} su @{username}; "
                                   f"rotazione pool @{current_account.username} → @{alt[0].username}")
                    current_account, current_client = alt
                    await asyncio.sleep(random.uniform(30 if is_soft else 15, 60 if is_soft else 30))
                else:
                    await asyncio.sleep(random.uniform(120, 240) if is_soft else 60)
                continue
            return None, e, current_account
    return None, RuntimeError("resolve retry esaurito"), current_account


async def resolve_imports(campaign_id: str) -> None:
    """Resolve all pending ImportedProfile rows into bio_scraped Followers."""
    _RESOLVING = (CampaignStatus.scraping, CampaignStatus.scraping_and_running, CampaignStatus.scraping_break)
    async with AsyncSessionLocal() as db:
        campaign = (await db.execute(select(Campaign).where(Campaign.id == campaign_id))).scalar_one_or_none()
        if not campaign:
            logger.error(f"[Import] Campaign {campaign_id} not found")
            return
        if campaign.source_type != "import":
            logger.warning(f"[Import] Campaign {campaign_id} non è di tipo import — skip")
            return
        if campaign.status not in _RESOLVING:
            logger.info(f"[Import] Campaign status='{campaign.status.value}' non risolvibile — skip stale retry")
            return
        if await is_halted(db):
            emit_event(campaign_id, "scrape_stopped", "Bot in pausa globale — risoluzione non avviata", level="warn")
            return

        emit_event(campaign_id, "scrape_start", "Risoluzione profili importati avviata...")
        pool = None
        try:
            # Approccio C: tutti gli account scraping/both pre-loggati nel pool, round-robin per-riga.
            pool = await ScrapingPool.build(db, campaign)
            emit_event(campaign_id, "scrape_start",
                       f"{pool.size} account scraping connessi, risolvo i profili...")

            since_break = 0
            resolved = 0
            while True:
                if await is_halted(db):
                    raise BotHaltedError("global kill-switch active")
                await db.refresh(campaign)
                if campaign.status not in _RESOLVING:
                    logger.info(f"[Import] Interrotto dall'utente dopo {resolved} profili")
                    return

                row = (await db.execute(
                    select(ImportedProfile).where(
                        ImportedProfile.campaign_id == campaign_id,
                        ImportedProfile.status == "pending",
                    ).limit(1)
                )).scalar_one_or_none()
                if row is None:
                    break  # finito

                # Round-robin: prossimo account con budget. None = tutti a cap.
                sel = pool.next(campaign)
                if sel is None:
                    campaign.status = CampaignStatus.paused
                    campaign.scrape_outcome = "scrape_capped"
                    campaign.updated_at = datetime.utcnow()
                    await db.commit()
                    emit_event(campaign_id, "scrape_stopped",
                               "Risoluzione in pausa: cap lookup giornaliero raggiunto su tutti gli account — riprende dopo il reset",
                               level="warn")
                    return
                current_account, current_client = sel

                info, err, account = await _resolve_one(db, campaign, row.username, pool, current_account, current_client)
                if info is not None:
                    await increment_scrape_lookup(db, account.id)
                    account.scrape_lookups_today = (account.scrape_lookups_today or 0) + 1
                status, create = classify_resolution(info, err)
                row.status = status
                row.error = (str(err)[:255] if err and status == "error" else None)
                # Log per-lead con l'account che ha eseguito il lookup (visibilita' round-robin).
                logger.info(
                    f"[Import] @{row.username} -> {status} via @{account.username} "
                    f"(lookups oggi: {account.scrape_lookups_today})"
                )
                if create and info is not None:
                    ig_pk = int(info.pk)
                    row.ig_user_id = ig_pk
                    dup = (await db.execute(select(Follower).where(
                        Follower.campaign_id == campaign_id, Follower.ig_user_id == ig_pk,
                    ))).scalar_one_or_none()
                    if dup is None:
                        contacts = extract_contacts(info)
                        biography = getattr(info, "biography", None) or None
                        db.add(Follower(
                            campaign_id=campaign_id,
                            ig_user_id=ig_pk,
                            username=info.username,
                            full_name=getattr(info, "full_name", None),
                            biography=biography,
                            is_private=getattr(info, "is_private", False),
                            is_verified=getattr(info, "is_verified", False),
                            follower_count=getattr(info, "follower_count", None),
                            following_count=getattr(info, "following_count", None),
                            external_url=contacts.external_url,
                            profile_pic_url=str(info.profile_pic_url) if getattr(info, "profile_pic_url", None) else None,
                            phone=contacts.phone,
                            email=contacts.email,
                            whatsapp=contacts.whatsapp,
                            bio_links=json.dumps(contacts.bio_links) if contacts.bio_links else None,
                            contact_source=json.dumps(contacts.sources) if contacts.sources else None,
                            status=FollowerStatus.bio_scraped,
                        ))
                        resolved += 1
                        await db.commit()
                        await upsert_lead(
                            db,
                            ig_user_id=ig_pk,
                            username=info.username,
                            full_name=getattr(info, "full_name", None),
                            biography=biography,
                            contacts=contacts,
                            campaign=campaign,
                            account=account,
                        )
                await db.commit()

                since_break += 1
                emit_event(campaign_id, "scrape_batch", f"Risolti {resolved} profili (ultimo: @{row.username} → {status})")

                # delay tra call (riusa bio_fetch_delay)
                dmin = getattr(campaign, "bio_fetch_delay_min", 5.0) or 5.0
                dmax = getattr(campaign, "bio_fetch_delay_max", 8.0) or 8.0
                await asyncio.sleep(random.uniform(dmin, dmax))

                # session break configurabile
                size = getattr(campaign, "scrape_session_size", 250)
                if since_break >= size:
                    bmin = getattr(campaign, "scrape_break_minutes_min", 30)
                    bmax = getattr(campaign, "scrape_break_minutes_max", 45)
                    minutes = random.uniform(bmin, bmax)
                    wake = datetime.utcnow() + timedelta(minutes=minutes)
                    prev = campaign.status.value
                    campaign.scrape_break_prev_status = prev
                    campaign.status = CampaignStatus.scraping_break
                    campaign.scrape_break_until = wake
                    await db.commit()
                    await pool.save_sessions(db)  # persisti le sessioni di tutti gli account prima della pausa
                    emit_event(campaign_id, "scrape_break", f"Pausa sessione ({int(minutes)} min) dopo {resolved} profili")
                    while datetime.utcnow() < wake:
                        await asyncio.sleep(10)
                        if await is_halted(db):
                            raise BotHaltedError("global kill-switch active")
                        await db.refresh(campaign)
                        if campaign.status != CampaignStatus.scraping_break:
                            break
                    if campaign.status == CampaignStatus.scraping_break:
                        campaign.status = CampaignStatus(prev)
                        campaign.scrape_break_until = None
                        campaign.scrape_break_prev_status = None
                        await db.commit()
                        emit_event(campaign_id, "scrape_resume", "Pausa terminata, risoluzione ripresa")
                    since_break = 0
                    await db.refresh(campaign)
                    if campaign.status not in _RESOLVING:
                        return

            # Completato
            await db.refresh(campaign)
            from sqlalchemy import func as sa_func
            total = await db.scalar(select(sa_func.count(Follower.id)).where(Follower.campaign_id == campaign_id)) or 0
            if campaign.status == CampaignStatus.scraping_and_running:
                campaign.status = CampaignStatus.running
            elif campaign.status in _RESOLVING:
                campaign.status = CampaignStatus.completed if not campaign.messaging_enabled else CampaignStatus.ready
            campaign.total_followers = total
            campaign.messages_pending = total
            campaign.scrape_outcome = "completed"
            campaign.scrape_completed_at = datetime.utcnow()
            campaign.updated_at = datetime.utcnow()
            db.add(ActivityLog(campaign_id=campaign_id, action="import_resolved",
                               details=json.dumps({"resolved": resolved, "total": total})))
            await db.commit()
            emit_event(campaign_id, "scrape_complete", f"Risoluzione completata: {total} profili pronti.")

        except BotHaltedError:
            await db.refresh(campaign)
            campaign.scrape_outcome = "partial"
            campaign.updated_at = datetime.utcnow()
            await db.commit()
            emit_event(campaign_id, "scrape_stopped", "Bot in pausa globale — risoluzione interrotta", level="warn")
        except ScrapingPoolEmpty as e:
            logger.error(f"[Import] resolve non avviato: {e}")
            campaign.status = CampaignStatus.error
            await db.commit()
            emit_event(campaign_id, "scrape_stopped", f"Risoluzione non avviata: {e}", level="error")
        except ScraperError as e:
            logger.error(f"[Import] {e}")
            campaign.status = CampaignStatus.error
            await db.commit()
            emit_event(campaign_id, "scrape_stopped", str(e), level="error")
        except Exception as e:
            logger.exception(f"[Import] resolve failed for {campaign_id}: {e}")
            campaign.status = CampaignStatus.error
            await db.commit()
            emit_event(campaign_id, "scrape_stopped", f"Errore risoluzione: {str(e)[:120]}", level="error")
        finally:
            if pool is not None:
                try:
                    await pool.save_sessions(db)
                except Exception as exc:
                    logger.warning(f"[Import] save_sessions finale fallito: {exc}")
                await pool.release()
