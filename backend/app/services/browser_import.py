"""Risoluzione lista IMPORTATA via BROWSER (Patchright) — gemello di import_resolver
per il motore `bio_engine='browser'`, con FAN-OUT per account (come la Fase Bio browser).

Perche' esiste: `import_resolver.resolve_imports` risolve ogni username importato con
`user_info_by_username_v1` (API instagrapi) — 1 chiamata prende pk+bio ma espone il
"pattern API nudo" su device sintetico (vedi memory [[botoutbound-checkpoint-pattern-api]]).
Quando la campagna e' `source_type=import` E `bio_engine=browser`, questo modulo fa lo
STESSO lavoro (username -> Follower `bio_scraped`) ma aprendo ogni profilo in un browser
reale: nessuna chiamata API instagrapi, nessun consumo del cap.

FAN-OUT (come `browser_bio.enqueue_browser_bio_workers` + `scrape_bios_browser_session`):
un task ARQ per account scraping, partenze sfalsate -> piu' sessioni browser in parallelo
(una finestra per account). Ogni sessione pesca profili DIVERSI dalla lista importata via
un claim atomico (status `pending` -> `resolving`, con recupero degli stale): due account
non prendono mai lo stesso profilo. Nessuna row-lock in tabella = nessuna migration.

Riuso: gli stessi primitivi di cattura/pacing/soft-block/challenge del path Fase-Bio-browser
(`browser_bio`), lo stesso `extract_contacts` + `upsert_lead`. Differenza chiave vs
`scrape_bios_browser_session`: li' i profili sono Follower(pending) gia' in DB creati dalla
Fase Lista; qui la sorgente sono ImportedProfile(pending) e il Follower va CREATO.
"""
import json
import random
from datetime import datetime, timedelta

import arq
from loguru import logger
from sqlalchemy import func, select, update

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.activity_log import ActivityLog
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.models.imported_profile import ImportedProfile
from app.services.bot_state_service import is_halted
from app.services.global_contact_service import upsert_lead
from app.services.work_enqueue import arq_redis_settings, ARQ_MAIN_QUEUE
from app.utils.contact_extract import extract_contacts
from app.utils.events import emit as emit_event
from app.utils.exceptions import (
    AccountBannedError, AccountChallengeError, AccountSessionExpiredError,
)
from app.browser.context_manager import BrowserSession
from app.services.scrape_bios import pick_session_cap
from app.services.browser_bio import (
    MAX_SESSION_ITERATIONS_MULTIPLIER,
    _capture_web_profile_info,
    _fetch_public_contact_inpage,
    _isolate_account_and_pause,
    _pause_campaign_soft_block,
    _scraping_accounts_of_campaign,
    _soft_block_incr,
    _soft_block_reset,
    human_profile_pause,
    maybe_micro_scroll,
    web_user_to_shim,
)

# Stati "in risoluzione" — identici a import_resolver._RESOLVING.
_RESOLVING = (
    CampaignStatus.scraping,
    CampaignStatus.scraping_and_running,
    CampaignStatus.scraping_break,
)

# Stato transiente di claim: una ImportedProfile presa in carico da una sessione ma non
# ancora risolta. Invisibile al path API (che interroga status='pending') e allo start
# guard (idem). Se il worker muore, il claim viene recuperato (updated_at troppo vecchio).
_RESOLVING_ROW = "resolving"


async def resolve_and_store_bio_browser(row, campaign, db, browser_session) -> tuple[str, Exception | None]:
    """Risolve UN ImportedProfile via browser e CREA il Follower(bio_scraped).

    Gemello di `browser_bio.fetch_and_store_bio_browser`, ma invece di aggiornare un
    Follower esistente lo crea da zero (come fa `import_resolver` col path API). Il pk
    arriva dal `web_profile_info` catturato in-page: NON serve nessuna lookup API per
    ottenerlo (il browser apre il profilo per username). email/telefono business dal
    fetch in-page `/info/` (come il path follower).

    Ritorna (outcome, err):
      'done'       -> Follower creato (o gia' esistente): row -> 'resolved'|'private'
      'not_found'  -> profilo inesistente: row -> 'not_found'
      'error'      -> parsing/HTTP non recuperabile: row -> 'error'
      'soft_block' -> 429/401/403 (web_profile_info o /info/): row NON marcata (il chiamante
                      rilascia il claim -> torna 'pending' per il retry)
      'network'    -> pagina/rete giu': row NON marcata (il chiamante rilascia il claim)
    """
    username = row.username
    try:
        raw_page = await browser_session.page._get_page()
    except Exception as e:
        return "network", e

    try:
        user = await _capture_web_profile_info(raw_page, username)
    except Exception as e:
        es = str(e).lower()
        if any(k in es for k in ("timeout", "net::", "connection", "proxy", "closed")):
            return "network", e
        return "error", e

    if user is None:
        row.status = "not_found"
        row.error = None
        row.updated_at = datetime.utcnow()
        await db.commit()
        return "not_found", None

    if isinstance(user, dict) and user.get("__status"):
        st = user["__status"]
        if st in (429, 401, 403):
            return "soft_block", Exception(f"web_profile_info HTTP {st}")
        row.status = "error"
        row.error = f"web_profile_info HTTP {st}"
        row.updated_at = datetime.utcnow()
        await db.commit()
        return "error", Exception(f"web_profile_info HTTP {st}")

    shim = web_user_to_shim(user)
    if shim.pk is None:
        row.status = "error"
        row.error = "no_pk"
        row.updated_at = datetime.utcnow()
        await db.commit()
        return "error", None

    # Arricchimento contatti business via /info/ in-page (identico a fetch_and_store_bio_browser):
    # web_profile_info torna business_email=null, i contatti veri stanno su /api/v1/users/{pk}/info/.
    if settings.bio_browser_contact_info_enabled:
        info = await _fetch_public_contact_inpage(raw_page, shim.pk)
        if isinstance(info, dict) and info.get("__rate_limited"):
            return "soft_block", Exception(f"/info/ HTTP {info['__rate_limited']}")
        if info:
            shim.public_email = info.get("public_email") or shim.public_email
            shim.public_phone_number = info.get("public_phone_number") or shim.public_phone_number
            shim.contact_phone_number = info.get("contact_phone_number") or shim.contact_phone_number
            shim.public_phone_country_code = (
                info.get("public_phone_country_code") or shim.public_phone_country_code
            )

    contacts = extract_contacts(shim)
    ig_pk = int(shim.pk)

    dup = (await db.execute(
        select(Follower).where(
            Follower.campaign_id == campaign.id,
            Follower.ig_user_id == ig_pk,
        )
    )).scalar_one_or_none()

    ext = shim.external_url
    if dup is None:
        db.add(Follower(
            campaign_id=campaign.id,
            ig_user_id=ig_pk,
            username=shim.username or username,
            full_name=shim.full_name,
            biography=shim.biography or None,
            is_private=bool(shim.is_private),
            is_verified=bool(shim.is_verified),
            follower_count=shim.follower_count,
            following_count=shim.following_count,
            external_url=contacts.external_url or (str(ext) if ext else None),
            profile_pic_url=None,
            phone=contacts.phone,
            email=contacts.email,
            whatsapp=contacts.whatsapp,
            bio_links=json.dumps(contacts.bio_links) if contacts.bio_links else None,
            contact_source=json.dumps(contacts.sources) if contacts.sources else None,
            status=FollowerStatus.bio_scraped,
        ))

    row.ig_user_id = ig_pk
    row.status = "private" if shim.is_private else "resolved"
    row.error = None
    row.updated_at = datetime.utcnow()
    await db.commit()

    if dup is None:
        await upsert_lead(
            db,
            ig_user_id=ig_pk,
            username=shim.username or username,
            full_name=shim.full_name,
            biography=shim.biography or None,
            contacts=contacts,
            campaign=campaign,
            account=None,
        )

    logger.info(f"[ImportBrowser] @{username} -> {row.status} via browser (no cap API)")
    return "done", None


async def _resilient_mark_import(db, import_id: str, *, status: str, error: str | None = None) -> None:
    """Marca una ImportedProfile via UPDATE-by-id, resiliente a una sessione avvelenata
    da un commit fallito a monte (mirror di browser_bio._resilient_release)."""
    try:
        await db.rollback()
    except Exception:
        pass
    vals = {"status": status, "updated_at": datetime.utcnow()}
    if error is not None:
        vals["error"] = error
    await db.execute(update(ImportedProfile).where(ImportedProfile.id == import_id).values(**vals))
    await db.commit()


async def _release_import_claim(db, import_id: str) -> None:
    """Rilascia un claim (resolving -> pending) cosi' la riga verra' ritentata. Resiliente
    a una sessione avvelenata: rollback preventivo + UPDATE-by-id condizionato su 'resolving'
    (non sovrascrive una riga gia' passata a terminale da un'altra transazione)."""
    try:
        await db.rollback()
    except Exception:
        pass
    await db.execute(
        update(ImportedProfile)
        .where(ImportedProfile.id == import_id, ImportedProfile.status == _RESOLVING_ROW)
        .values(status="pending", updated_at=datetime.utcnow())
    )
    await db.commit()


async def claim_next_pending_import(db, campaign_id: str, account_id: str):
    """Claima atomicamente una ImportedProfile pending per questa sessione (status-flip
    pending -> resolving). Prima recupera gli stale (resolving da una sessione morta:
    updated_at oltre il timeout). Ritorna la riga claimata o None. Safe con piu' account
    paralleli (optimistic: SELECT poi UPDATE guarded su status, retry sulla race)."""
    from app.services.campaign_orchestrator import LOCK_TIMEOUT_MINUTES

    stale_cutoff = datetime.utcnow() - timedelta(minutes=LOCK_TIMEOUT_MINUTES)
    await db.execute(
        update(ImportedProfile).where(
            ImportedProfile.campaign_id == campaign_id,
            ImportedProfile.status == _RESOLVING_ROW,
            ImportedProfile.updated_at < stale_cutoff,
        ).values(status="pending", updated_at=datetime.utcnow())
    )
    await db.commit()

    for _ in range(25):  # ritenta se un altro account claima tra SELECT e UPDATE
        row = (await db.execute(
            select(ImportedProfile).where(
                ImportedProfile.campaign_id == campaign_id,
                ImportedProfile.status == "pending",
            ).limit(1)
        )).scalar_one_or_none()
        if row is None:
            return None
        claim = await db.execute(
            update(ImportedProfile).where(
                ImportedProfile.id == row.id,
                ImportedProfile.status == "pending",
            ).values(status=_RESOLVING_ROW, updated_at=datetime.utcnow())
        )
        await db.commit()
        if claim.rowcount == 1:
            await db.refresh(row)
            return row
    return None


async def _pending_import_count(db, campaign_id: str) -> int:
    """Solo 'pending' (per lo start guard e il dispatch)."""
    return await db.scalar(
        select(func.count()).select_from(ImportedProfile).where(
            ImportedProfile.campaign_id == campaign_id,
            ImportedProfile.status == "pending",
        )
    ) or 0


async def _open_import_count(db, campaign_id: str) -> int:
    """'pending' + 'resolving' (in volo su altri account): la campagna e' finita solo
    quando entrambi sono 0 (l'ultimo account che svuota il pool completa)."""
    return await db.scalar(
        select(func.count()).select_from(ImportedProfile).where(
            ImportedProfile.campaign_id == campaign_id,
            ImportedProfile.status.in_(("pending", _RESOLVING_ROW)),
        )
    ) or 0


async def _complete_import_browser(campaign_id: str) -> bool:
    """Completamento (mirror di browser_bio._maybe_complete_browser_bio + resolve_imports
    finale): quando NON restano ImportedProfile aperte (pending+resolving) porta la campagna
    a ready/completed. UPDATE atomico condizionato su status IN _RESOLVING con rowcount ->
    SOLO un account (l'ultimo) vince la transizione ed emette l'evento."""
    async with AsyncSessionLocal() as db:
        campaign = (await db.execute(
            select(Campaign).where(Campaign.id == campaign_id)
        )).scalar_one_or_none()
        if campaign is None or campaign.status not in _RESOLVING:
            return False
        if await _open_import_count(db, campaign_id) > 0:
            return False  # altri account stanno ancora smaltendo

        total = await db.scalar(
            select(func.count(Follower.id)).where(Follower.campaign_id == campaign_id)
        ) or 0
        new_status = (
            CampaignStatus.running if campaign.status == CampaignStatus.scraping_and_running
            else (CampaignStatus.completed if not campaign.messaging_enabled else CampaignStatus.ready)
        )
        result = await db.execute(
            update(Campaign).where(
                Campaign.id == campaign_id,
                Campaign.status.in_(list(_RESOLVING)),
            ).values(
                status=new_status,
                total_followers=total,
                messages_pending=total,
                scrape_outcome="completed",
                scrape_completed_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
        )
        await db.commit()
        if result.rowcount == 1:
            db.add(ActivityLog(
                campaign_id=campaign_id, action="import_resolved",
                details=json.dumps({"total": total, "engine": "browser"}),
            ))
            await db.commit()
            emit_event(campaign_id, "scrape_complete", f"Risoluzione (browser) completata: {total} profili pronti.")
            return True
        return False


# --------------------------------------------------------------------------- #
# Fan-out: un task ARQ per account scraping (mirror di enqueue_browser_bio_workers)
# --------------------------------------------------------------------------- #
def browser_import_job_id(campaign_id: str, account_id: str) -> str:
    return f"importbrowser:{campaign_id}:{account_id}"


def browser_import_redis_keys(campaign_id: str, account_id: str) -> tuple[str, str, str]:
    job_id = browser_import_job_id(campaign_id, account_id)
    return (
        f"arq:job:{job_id}",
        f"arq:retry:{job_id}",
        f"arq:in-progress:{job_id}",
    )


async def enqueue_browser_import_workers(campaign_id: str) -> int:
    """Fan-out: un task per account scraping, stagger crescente via _defer_by, _job_id
    deterministico (dedup). Identica disciplina di enqueue_browser_bio_workers: un job
    'in-progress' NON viene ne' cancellato ne' ri-accodato (niente secondo worker sullo
    stesso account). Ritorna il numero di account ORA schedulati (nuovi + gia' in corso)."""
    accounts = await _scraping_accounts_of_campaign(campaign_id)
    if not accounts:
        return 0
    redis = await arq.create_pool(arq_redis_settings())
    lo = min(settings.bio_browser_stagger_min_s, settings.bio_browser_stagger_max_s)
    hi = max(settings.bio_browser_stagger_min_s, settings.bio_browser_stagger_max_s)
    n = 0
    try:
        for idx, (account_id, _username) in enumerate(accounts):
            job_id = browser_import_job_id(campaign_id, account_id)
            job_key, retry_key, in_progress_key = browser_import_redis_keys(campaign_id, account_id)
            if await redis.exists(in_progress_key):
                logger.info(f"[ImportBrowser] {job_id} gia' in esecuzione — skip enqueue duplicato")
                n += 1
                continue
            await redis.delete(job_key, retry_key)
            defer = 0 if idx == 0 else int(random.uniform(lo, hi) * idx)
            await redis.enqueue_job(
                "browser_import_account_task",
                campaign_id,
                account_id,
                _job_id=job_id,
                _defer_by=defer,
                _queue_name=ARQ_MAIN_QUEUE,
            )
            n += 1
    finally:
        await redis.aclose()
    return n


async def resolve_imports_browser_session(campaign_id: str, account_id: str) -> int | None:
    """Una mini-sessione browser per UN account: apre, risolve fino a `cap` profili
    claimati dalla lista importata (pool disgiunto via claim_next_pending_import), chiude.
    Ritorna i secondi di defer per la pausa lunga anti-block, o None se non c'e' piu'
    lavoro. Job corto (mai oltre job_timeout). Difensiva sui singoli profili."""
    async with AsyncSessionLocal() as db:
        campaign = (await db.execute(
            select(Campaign).where(Campaign.id == campaign_id)
        )).scalar_one_or_none()
        if campaign is None or campaign.status not in _RESOLVING or getattr(campaign, "bio_engine", "api") != "browser":
            return None
        if await is_halted(db):
            return None
        if await _open_import_count(db, campaign_id) == 0:
            await _complete_import_browser(campaign_id)
            return None

    cap = pick_session_cap(settings.bio_browser_session_cap_min, settings.bio_browser_session_cap_max)
    max_iterations = cap * MAX_SESSION_ITERATIONS_MULTIPLIER
    done_count = 0
    iterations = 0
    session = None
    reels_cadence_target = random.randint(
        settings.bio_browser_reels_every_min, settings.bio_browser_reels_every_max
    )
    profiles_since_reels_break = 0

    try:
        session = BrowserSession(account_id, headless=settings.bio_browser_headless)
        await session.open()
        # allow_login=False: lo scraping NON fa MAI login automatico (ban risk). Sessione
        # scaduta -> AccountSessionExpiredError -> isolamento (except sotto).
        await session.page.ensure_logged_in(account_id, allow_login=False)

        while done_count < cap and iterations < max_iterations:
            profile_is_private = False
            async with AsyncSessionLocal() as db:
                if await is_halted(db):
                    return None
                campaign = (await db.execute(
                    select(Campaign).where(Campaign.id == campaign_id)
                )).scalar_one_or_none()
                # Bail se lo status esce da _RESOLVING O se l'engine e' passato ad 'api':
                # uno switch browser->api lascia parked i task browser, che NON devono girare
                # in parallelo al loop API (footprint doppio + race sulle ImportedProfile).
                if campaign is None or campaign.status not in _RESOLVING or getattr(campaign, "bio_engine", "api") != "browser":
                    return None

                row = await claim_next_pending_import(db, campaign_id, account_id)
                if row is None:
                    if await _complete_import_browser(campaign_id):
                        return None  # pool esaurito -> campagna completata
                    # 'pending' vuoto ma restano righe 'resolving' (in volo su altri account,
                    # o ORFANE da una sessione morta): resta vivo e ritenta dopo la finestra
                    # stale (LOCK_TIMEOUT), cosi' il prossimo claim_next_pending_import le
                    # recupera. Senza questo defer la campagna resterebbe bloccata per sempre.
                    from app.services.campaign_orchestrator import LOCK_TIMEOUT_MINUTES
                    return LOCK_TIMEOUT_MINUTES * 60 + 60

                rid, uname = row.id, row.username
                try:
                    outcome, err = await resolve_and_store_bio_browser(row, campaign, db, session)
                except Exception as e:
                    logger.warning(f"[ImportBrowser] @{uname} errore inatteso ({e}) — marco error")
                    await _resilient_mark_import(db, rid, status="error", error=str(e)[:255])
                    outcome, err = "error", e

                # Privacy per il pacing (solo su 'done': row committata con successo).
                profile_is_private = outcome == "done" and getattr(row, "status", "") == "private"

                iterations += 1
                if outcome == "done":
                    done_count += 1
                    if done_count == 1:
                        await _soft_block_reset(campaign_id, account_id)
                    emit_event(campaign_id, "scrape_batch", f"Risolto @{uname} via browser")
                elif outcome in ("not_found", "error"):
                    emit_event(campaign_id, "scrape_progress", f"@{uname}: {outcome}", level="warn")
                elif outcome == "soft_block":
                    # Rilascia il claim (resolving -> pending) e fai backoff crescente;
                    # dopo N consecutivi -> pausa campagna (mirror del guard follower).
                    await _release_import_claim(db, rid)
                    logger.warning(f"[ImportBrowser] soft-block su @{uname}: {err} — backoff")
                    emit_event(campaign_id, "scrape_progress", f"@{uname}: soft-block (429) — backoff", level="warn")
                    n_sb = await _soft_block_incr(campaign_id, account_id)
                    if n_sb >= settings.bio_browser_soft_block_pause_threshold:
                        await _pause_campaign_soft_block(campaign_id, account_id, n_sb)
                        return None
                    base = random.randint(900, 1800)
                    return min(3600, base * n_sb)
                elif outcome == "network":
                    await _release_import_claim(db, rid)
                    logger.warning(f"[ImportBrowser] errore rete su @{uname}: {err} — retry breve")
                    return 180

            await maybe_micro_scroll(session, is_private=profile_is_private)
            profiles_since_reels_break += 1
            if profiles_since_reels_break >= reels_cadence_target:
                try:
                    n_reels = random.randint(
                        settings.bio_browser_reels_count_min,
                        settings.bio_browser_reels_count_max,
                    )
                    logger.info(f"[ImportBrowser] pausa attiva sui reel: {n_reels} reel")
                    await session.page.browse_reels(
                        n_reels,
                        dwell_min_s=settings.bio_browser_reels_dwell_min_s,
                        dwell_max_s=settings.bio_browser_reels_dwell_max_s,
                    )
                except Exception as e:
                    logger.warning(f"[ImportBrowser] pausa reel fallita ({type(e).__name__}: {e}) — ignorata")
                profiles_since_reels_break = 0
                reels_cadence_target = random.randint(
                    settings.bio_browser_reels_every_min, settings.bio_browser_reels_every_max
                )
            else:
                await human_profile_pause()

        # Mini-sessione finita: prova a completare (se questo account ha drenato il pool),
        # altrimenti defer per la pausa lunga anti-block.
        if await _complete_import_browser(campaign_id):
            return None
        if done_count >= cap:
            minutes = random.uniform(
                getattr(campaign, "scrape_break_minutes_min", 30) or 30,
                getattr(campaign, "scrape_break_minutes_max", 45) or 45,
            )
            emit_event(campaign_id, "scrape_break", f"Pausa risoluzione browser {int(minutes)} min")
            return max(60, int(minutes * 60))

        logger.info(
            f"[ImportBrowser] backstop iterazioni ({iterations}/{max_iterations}) "
            f"con {done_count}/{cap} risolti — defer breve"
        )
        return 60

    except (AccountChallengeError, AccountBannedError, AccountSessionExpiredError) as e:
        logger.error(f"[ImportBrowser] account fatale @{account_id[:8]}: {type(e).__name__} — isolo + pauso")
        await _isolate_account_and_pause(campaign_id, account_id, e)
        return None
    except Exception as e:
        logger.warning(f"[ImportBrowser] mini-sessione @{account_id[:8]} fallita ({type(e).__name__}: {e})")
        return 300
    finally:
        if session is not None:
            try:
                await session.close()
            except Exception:
                pass
