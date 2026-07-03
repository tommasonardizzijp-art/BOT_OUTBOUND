"""Fase Bio: estrae bio+contatti dai Follower(status=pending) gia' in lista."""
import asyncio
import random
import time
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import func, select

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.services.bot_state_service import is_halted
from app.services.scraping_pool import ScrapingPool, ScrapingPoolEmpty, ScrapingSlotsBusy
from app.services.scraper import fetch_and_store_bio, is_challenge_exception, isolate_challenged_account
from app.utils.exceptions import BotHaltedError, ScrapeBudgetError, SoftBlockError


# Ritenta lo STESSO profilo prima di skippare/pausare (assorbe blip transitori).
MAX_BIO_ATTEMPTS = 3
# Fallimenti di fila oltre i quali la run si ferma (problema sistemico, non profilo).
MAX_CONSECUTIVE_BIO_FAIL = 5
# Micro-yield: ogni quante bio estratte (o secondi di wall-clock) il job cede ad ARQ
# con un defer brevissimo. Spezza un job lungo in tanti job corti, cosi' la durata del
# SINGOLO job resta ben sotto job_timeout del worker (3600s) SENZA un cap hard sul
# totale dei lead. ~100 lookup x ~18s ~= 1800s, meta' del timeout. Invisibile
# all'utente: lo status resta 'scraping' e il job successivo riprende subito dai
# pending. Distinto dalla pausa lunga anti-block (scrape_session_size -> 30-45 min),
# che resta la cadenza "umana": questo serve SOLO a non sforare il timeout di ARQ.
MICRO_YIELD_EVERY = 100
MICRO_YIELD_MAX_SECONDS = 40 * 60


def bio_should_continue(target: int | None, done: int) -> bool:
    """True se la Fase Bio deve continuare dato il target e i gia' fatti."""
    if target is None:
        return True
    return done < target


async def scrape_bios(campaign_id: str) -> int | None:
    """Entry point Fase Bio. Chiamata dal worker.

    Ritorna i secondi di defer se ha colpito una pausa sessione (il worker
    solleva Retry(defer=...)); None se completata/interrotta.
    """
    async with AsyncSessionLocal() as db:
        campaign = (await db.execute(select(Campaign).where(Campaign.id == campaign_id))).scalar_one_or_none()
        if not campaign:
            return None
        if campaign.status not in (CampaignStatus.scraping, CampaignStatus.scraping_break):
            logger.info(f"[Bio] Stato '{campaign.status.value}' — skip stale retry")
            return None
        if await is_halted(db):
            from app.utils.events import emit as emit_event
            emit_event(campaign_id, "scrape_stopped", "Bot in pausa globale — bio non avviata", level="warn")
            return None
        # Resume da pausa sessione: il job rientra in scraping_break dopo il defer.
        if campaign.status == CampaignStatus.scraping_break:
            from app.utils.events import emit as emit_event
            campaign.status = CampaignStatus.scraping
            campaign.scrape_break_until = None
            campaign.scrape_break_prev_status = None
            campaign.updated_at = datetime.utcnow()
            await db.commit()
            emit_event(campaign_id, "scrape_resume", "Pausa bio terminata, ripresa")

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
        consecutive_fail = 0
        attempts: dict[str, int] = {}
        try:
            from app.utils.events import emit as emit_event

            pool = await ScrapingPool.build(db, campaign)
            emit_event(campaign_id, "scrape_start", f"Fase Bio avviata — target {campaign.bio_target or 'tutti i pending'}")
            # Cadenza pausa lunga anti-block ancorata a `done` (count bio_scraped,
            # persistito in DB): sopravvive ai micro-yield/restart, che azzerano i
            # contatori locali. Prossima pausa al primo multiplo di `size` oltre `done`.
            size = getattr(campaign, "scrape_session_size", 250) or 250
            next_long_break = ((done // size) + 1) * size
            # Contatori del SINGOLO job (azzerati a ogni restart): governano il micro-yield.
            processed_this_job = 0
            job_started = time.monotonic()

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
                    logger.warning(
                        f"[Bio] @{follower.username} via @{account.username if account else '?'} "
                        f"429/soft-block ({consecutive_soft}/3): {err}"
                    )
                    if consecutive_soft >= 3:
                        raise SoftBlockError("3 soft block consecutivi")
                    await asyncio.sleep(random.uniform(90, 180))
                    continue

                if outcome in ("network", "error"):
                    # Ritenta lo stesso profilo: con limit(1) senza ORDER BY la
                    # riga ri-selezionata e' la stessa (resta pending). Assorbe
                    # blip di rete o parse sporadici prima di decidere.
                    fid = follower.id
                    attempts[fid] = attempts.get(fid, 0) + 1
                    if attempts[fid] < MAX_BIO_ATTEMPTS:
                        backoff = random.uniform(5, 12) * attempts[fid]
                        logger.warning(
                            f"[Bio] @{follower.username} {outcome} "
                            f"(tentativo {attempts[fid]}/{MAX_BIO_ATTEMPTS}) — ritento tra {int(backoff)}s: {err}"
                        )
                        await asyncio.sleep(backoff)
                        continue

                    if outcome == "network":
                        # Connessione giu' (tethering/proxy): NON skippare profili
                        # buoni. Pausa la run preservando i pending; riavviabile da
                        # bios/start dopo il fix.
                        campaign.status = CampaignStatus.error
                        campaign.scrape_outcome = "scrape_network_error"
                        campaign.updated_at = datetime.utcnow()
                        await db.commit()
                        emit_event(
                            campaign_id, "scrape_stopped",
                            "Connessione persa (tethering/proxy?) — bio interrotta, riprendi dopo il fix",
                            level="error",
                        )
                        logger.error(f"[Bio] Rete giu' su @{follower.username} dopo {attempts[fid]} tentativi — pausa run a {done}")
                        return

                    # Profilo non parsabile (es. schema IG cambiato): skip e avanza.
                    # Questo evita il loop infinito sul medesimo follower pending.
                    follower.status = FollowerStatus.skipped
                    follower.skip_reason = (f"bio_error: {str(err)[:200]}" if err else "bio_error")
                    follower.updated_at = datetime.utcnow()
                    await db.commit()
                    consecutive_fail += 1
                    logger.warning(f"[Bio] @{follower.username} SKIP dopo {attempts[fid]} tentativi: {err}")
                    emit_event(campaign_id, "scrape_progress", f"@{follower.username} saltato: bio non recuperabile", level="warn")
                    if consecutive_fail >= MAX_CONSECUTIVE_BIO_FAIL:
                        # Troppi skip di fila => sistemico, fermati per non bruciare la lista.
                        campaign.status = CampaignStatus.error
                        campaign.scrape_outcome = "scrape_errors"
                        campaign.updated_at = datetime.utcnow()
                        await db.commit()
                        emit_event(
                            campaign_id, "scrape_stopped",
                            f"{consecutive_fail} bio fallite di fila — interrotta, controlla account/connessione",
                            level="error",
                        )
                        logger.error(f"[Bio] {consecutive_fail} fallimenti consecutivi — pausa run a {done}")
                        return
                    await asyncio.sleep(random.uniform(3, 8))
                    continue

                if outcome == "done":
                    consecutive_soft = 0
                    consecutive_fail = 0
                    done += 1
                    processed_this_job += 1
                    delay = random.uniform(
                        getattr(campaign, "bio_fetch_delay_min", 5.0) or 5.0,
                        getattr(campaign, "bio_fetch_delay_max", 8.0) or 8.0,
                    )
                    await asyncio.sleep(delay)

                    # Pausa lunga anti-block (cadenza "umana", invariata): ogni `size`
                    # bio totali. Dentro il ramo "done" (mai dopo uno skip) e ancorata a
                    # `done` cosi' non ri-scatta al rientro quando `done` resta sul confine.
                    if done >= next_long_break:
                        minutes = random.uniform(
                            getattr(campaign, "scrape_break_minutes_min", 30),
                            getattr(campaign, "scrape_break_minutes_max", 45),
                        )
                        seconds = int(minutes * 60)
                        campaign.scrape_break_prev_status = CampaignStatus.scraping.value
                        campaign.status = CampaignStatus.scraping_break
                        campaign.scrape_break_until = datetime.utcnow() + timedelta(seconds=seconds)
                        campaign.updated_at = datetime.utcnow()
                        await db.commit()
                        emit_event(campaign_id, "scrape_break", f"Pausa bio {int(minutes)} min dopo {done}")
                        logger.info(f"[Bio] Pausa sessione {int(minutes)}min dopo {done} bio — defer job")
                        return seconds

                # Micro-yield: cede il job ad ARQ ben prima di job_timeout (3600s).
                # Defer brevissimo, status RESTA 'scraping' (niente *_break, nessun
                # evento utente): il job successivo riprende subito dai pending. Spezza
                # il job lungo, non e' una pausa percepita. Conta solo le bio riuscite
                # (processed_this_job): durante uno streak di fallimenti agisce invece il
                # guard consecutive_fail; il backstop wall-clock copre i lookup lenti.
                if (
                    processed_this_job >= MICRO_YIELD_EVERY
                    or (time.monotonic() - job_started) >= MICRO_YIELD_MAX_SECONDS
                ):
                    logger.info(
                        f"[Bio] Micro-yield dopo {processed_this_job} bio / "
                        f"{int(time.monotonic() - job_started)}s — defer job "
                        f"(status resta scraping, {done} totali)"
                    )
                    return 2

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

        except ScrapingSlotsBusy:
            # Job bios duplicato: gli slot li tiene gia' il job legittimo. Esco
            # senza toccare lo stato campagna (NON metto error, altrimenti uccido
            # il job vivo che al refresh vedrebbe 'error' e si fermerebbe).
            logger.info(f"[Bio] Slot account occupati da altro job — esco no-op ({done} fatti)")
            return

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
                from app.utils.events import emit as emit_event
                logger.error(f"[Bio] Errore {campaign_id}: {e}")
                campaign.status = CampaignStatus.error
                campaign.updated_at = datetime.utcnow()
                await db.commit()
                emit_event(
                    campaign_id, "scrape_stopped",
                    f"Fase Bio interrotta da errore inatteso: {e}",
                    level="error",
                )

        finally:
            if pool is not None:
                try:
                    await pool.save_sessions(db)
                except Exception as exc:
                    logger.warning(f"[Bio] save_sessions fallito: {exc}")
                await pool.release()
