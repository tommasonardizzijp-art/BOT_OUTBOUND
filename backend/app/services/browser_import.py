"""Risoluzione lista IMPORTATA via BROWSER (Patchright) — gemello di import_resolver
per il motore `bio_engine='browser'`.

Perche' esiste: `import_resolver.resolve_imports` risolve ogni username importato con
`user_info_by_username_v1` (API instagrapi) — 1 chiamata prende pk+bio ma espone il
"pattern API nudo" su device sintetico (vedi memory [[botoutbound-checkpoint-pattern-api]]).
Quando la campagna e' `source_type=import` E `bio_engine=browser`, questo modulo fa lo
STESSO lavoro (username -> Follower `bio_scraped`) ma aprendo ogni profilo in un browser
reale: nessuna chiamata API instagrapi, nessun consumo del cap scrape_daily_limit.

Riuso: gli stessi primitivi di cattura del path Fase-Bio-browser
(`browser_bio._capture_web_profile_info` / `web_user_to_shim` / `_fetch_public_contact_inpage`),
lo stesso `extract_contacts` + `upsert_lead` di `import_resolver`. Differenza chiave vs
`scrape_bios_browser_session`: li' i profili sono Follower(pending) gia' in DB creati dalla
Fase Lista; qui la sorgente sono ImportedProfile(pending) e il Follower va CREATO.

Concorrenza: gira come JOB SINGOLO (job_id `resolve:{cid}`, dedup ARQ) — a differenza del
fan-out per-account della Fase Bio browser. Quindi NESSUN lock su ImportedProfile serve:
un solo worker seleziona/segna le righe. Gli account scraping vengono ruotati NEL TEMPO
(uno per mini-sessione, non in parallelo): footprint piu' basso, coerente col fatto che il
motore browser e' la modalita' "prudente e lenta".
"""
import json
import random
from datetime import datetime

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


async def resolve_and_store_bio_browser(row, campaign, db, browser_session) -> tuple[str, Exception | None]:
    """Risolve UN ImportedProfile via browser e CREA il Follower(bio_scraped).

    Gemello di `browser_bio.fetch_and_store_bio_browser`, ma invece di aggiornare un
    Follower esistente lo crea da zero (come fa `import_resolver` col path API). Il pk
    arriva dal `web_profile_info` catturato in-page: NON serve nessuna lookup API per
    ottenerlo (il browser apre il profilo per username).

    Ritorna (outcome, err):
      'done'       -> Follower creato (o gia' esistente): row -> 'resolved'|'private'
      'not_found'  -> profilo inesistente: row -> 'not_found'
      'error'      -> parsing/HTTP non recuperabile: row -> 'error'
      'soft_block' -> 429/401/403 (web_profile_info o /info/): row RESTA 'pending' (retry)
      'network'    -> pagina/rete giu': row RESTA 'pending' (retry)
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
        # Nessun dato: profilo inesistente o parsing a vuoto. Miss terminale non fatale.
        row.status = "not_found"
        row.error = None
        row.updated_at = datetime.utcnow()
        await db.commit()
        return "not_found", None

    if isinstance(user, dict) and user.get("__status"):
        st = user["__status"]
        # 429/401/403 dal web = soft-block/rate: il chiamante rallenta o pausa. NON
        # marcare la row (resta pending, verra' ritentata).
        if st in (429, 401, 403):
            return "soft_block", Exception(f"web_profile_info HTTP {st}")
        row.status = "error"
        row.error = f"web_profile_info HTTP {st}"
        row.updated_at = datetime.utcnow()
        await db.commit()
        return "error", Exception(f"web_profile_info HTTP {st}")

    shim = web_user_to_shim(user)
    if shim.pk is None:
        # Senza pk non possiamo creare il Follower (ig_user_id NOT NULL + unique).
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
            # /info/ rate-limitato: propaga soft_block (NON ingoiare — vedi bug INFO-1).
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

    # Dedup: se un Follower con questo pk esiste gia' per la campagna (username duplicato
    # nel file, o gia' risolto), non re-inserire — l'unique (campaign_id, ig_user_id)
    # solleverebbe. Marca comunque la row risolta.
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
            profile_pic_url=None,  # web_profile_info shim non lo espone
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
            account=None,  # via browser: nessun account API attribuibile alla lookup
        )

    logger.info(f"[ImportBrowser] @{username} -> {row.status} via browser (no cap API)")
    return "done", None


async def _resilient_mark_import(db, import_id: str, *, status: str, error: str | None = None) -> None:
    """Marca una ImportedProfile via UPDATE-by-id, resiliente a una sessione avvelenata
    da un commit fallito a monte (mirror di browser_bio._resilient_release): rollback
    preventivo + UPDATE che non tocca l'oggetto ORM (inservibile dopo un flush fallito)."""
    try:
        await db.rollback()
    except Exception:
        pass
    vals = {"status": status, "updated_at": datetime.utcnow()}
    if error is not None:
        vals["error"] = error
    await db.execute(update(ImportedProfile).where(ImportedProfile.id == import_id).values(**vals))
    await db.commit()


async def _pending_import_count(db, campaign_id: str) -> int:
    return await db.scalar(
        select(func.count()).select_from(ImportedProfile).where(
            ImportedProfile.campaign_id == campaign_id,
            ImportedProfile.status == "pending",
        )
    ) or 0


async def _complete_import_browser(campaign_id: str) -> bool:
    """Completamento: nessun ImportedProfile pending -> porta la campagna a
    ready/completed (mirror di import_resolver.resolve_imports righe 270-286). Job
    singolo: nessuna race. Ritorna True se ha transitato lo stato."""
    async with AsyncSessionLocal() as db:
        campaign = (await db.execute(
            select(Campaign).where(Campaign.id == campaign_id)
        )).scalar_one_or_none()
        if campaign is None or campaign.status not in _RESOLVING:
            return False
        if await _pending_import_count(db, campaign_id) > 0:
            return False  # ancora lavoro: non completare

        total = await db.scalar(
            select(func.count(Follower.id)).where(Follower.campaign_id == campaign_id)
        ) or 0
        if campaign.status == CampaignStatus.scraping_and_running:
            campaign.status = CampaignStatus.running
        else:
            campaign.status = (
                CampaignStatus.completed if not campaign.messaging_enabled else CampaignStatus.ready
            )
        campaign.total_followers = total
        campaign.messages_pending = total
        campaign.scrape_outcome = "completed"
        campaign.scrape_completed_at = datetime.utcnow()
        campaign.updated_at = datetime.utcnow()
        db.add(ActivityLog(
            campaign_id=campaign_id, action="import_resolved",
            details=json.dumps({"total": total, "engine": "browser"}),
        ))
        await db.commit()
        emit_event(campaign_id, "scrape_complete", f"Risoluzione (browser) completata: {total} profili pronti.")
        return True


async def resolve_imports_browser(campaign_id: str) -> int | None:
    """Entry point del resolver import via browser. UNA mini-sessione su UN account
    scraping: apre il browser, risolve fino a `cap` profili claimati dalla lista
    importata, chiude. Ritorna i secondi di defer per la pausa lunga anti-block
    (il task solleva Retry(defer=...)), oppure None se non c'e' piu' lavoro.

    Job corto (mai oltre job_timeout). Difensivo sui singoli profili. Ruota l'account
    tra una mini-sessione e l'altra (scelta random tra gli scraping attivi)."""
    async with AsyncSessionLocal() as db:
        campaign = (await db.execute(
            select(Campaign).where(Campaign.id == campaign_id)
        )).scalar_one_or_none()
        if campaign is None or campaign.status not in _RESOLVING:
            return None
        if await is_halted(db):
            emit_event(campaign_id, "scrape_stopped", "Bot in pausa globale — risoluzione browser non avviata", level="warn")
            return None
        if await _pending_import_count(db, campaign_id) == 0:
            await _complete_import_browser(campaign_id)
            return None

    accounts = await _scraping_accounts_of_campaign(campaign_id)
    if not accounts:
        async with AsyncSessionLocal() as db:
            campaign = (await db.execute(
                select(Campaign).where(Campaign.id == campaign_id)
            )).scalar_one_or_none()
            if campaign is not None and campaign.status in _RESOLVING:
                campaign.status = CampaignStatus.error
                campaign.scrape_outcome = "scrape_no_account"
                campaign.updated_at = datetime.utcnow()
                await db.commit()
        emit_event(campaign_id, "scrape_stopped", "Nessun account scraping attivo per il motore browser", level="error")
        return None

    account_id, username = random.choice(accounts)

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
        # allow_login=False: lo scraping NON fa MAI login automatico (ban risk).
        await session.page.ensure_logged_in(account_id, allow_login=False)
        emit_event(campaign_id, "scrape_start", f"Risoluzione via browser su @{username}")

        while done_count < cap and iterations < max_iterations:
            async with AsyncSessionLocal() as db:
                if await is_halted(db):
                    return None
                campaign = (await db.execute(
                    select(Campaign).where(Campaign.id == campaign_id)
                )).scalar_one_or_none()
                if campaign is None or campaign.status not in _RESOLVING:
                    return None

                row = (await db.execute(
                    select(ImportedProfile).where(
                        ImportedProfile.campaign_id == campaign_id,
                        ImportedProfile.status == "pending",
                    ).limit(1)
                )).scalar_one_or_none()
                if row is None:
                    await _complete_import_browser(campaign_id)
                    return None  # lista importata esaurita

                # Cattura id/username SUBITO: se il commit dentro resolve_and_store
                # fallisce, l'oggetto ORM `row` diventa inservibile (PendingRollback).
                rid, uname = row.id, row.username
                try:
                    outcome, err = await resolve_and_store_bio_browser(row, campaign, db, session)
                except Exception as e:
                    logger.warning(f"[ImportBrowser] @{uname} errore inatteso ({e}) — marco error")
                    # Marca error via UPDATE-by-id per non ri-selezionare lo stesso pending.
                    await _resilient_mark_import(db, rid, status="error", error=str(e)[:255])
                    outcome, err = "error", e

                # Privacy per il pacing dello scroll: nota SOLO su 'done' (row committata
                # con successo -> safe leggere row.status, come il path follower legge
                # follower.is_private dopo un commit riuscito). resolve_and_store setta
                # row.status='private' per i profili privati risolti.
                profile_is_private = outcome == "done" and getattr(row, "status", "") == "private"

                iterations += 1
                if outcome == "done":
                    done_count += 1
                    if done_count == 1:
                        await _soft_block_reset(campaign_id, account_id)
                    emit_event(campaign_id, "scrape_batch", f"Risolto @{uname} via browser")
                elif outcome in ("not_found", "error"):
                    # row gia' marcata dentro resolve_and_store_bio_browser: avanza.
                    emit_event(campaign_id, "scrape_progress", f"@{uname}: {outcome}", level="warn")
                elif outcome == "soft_block":
                    # row resta pending (retry): backoff crescente, dopo N -> pausa campagna.
                    logger.warning(f"[ImportBrowser] soft-block su @{uname}: {err} — backoff")
                    emit_event(campaign_id, "scrape_progress", f"@{uname}: soft-block (429) — backoff", level="warn")
                    n_sb = await _soft_block_incr(campaign_id, account_id)
                    if n_sb >= settings.bio_browser_soft_block_pause_threshold:
                        await _pause_campaign_soft_block(campaign_id, account_id, n_sb)
                        return None
                    base = random.randint(900, 1800)
                    return min(3600, base * n_sb)
                elif outcome == "network":
                    logger.warning(f"[ImportBrowser] errore rete su @{uname}: {err} — retry breve")
                    return 180

            # Pacing umano tra un profilo e l'altro (soft_block/network hanno gia' fatto
            # return). Passa la privacy reale (scroll breve solo-header sui privati) per
            # parita' col path follower (scrape_bios_browser_session).
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

        # Mini-sessione finita: prova a completare (se questo account ha drenato la
        # lista), altrimenti defer per la pausa lunga anti-block e riprendi con un
        # altro account alla prossima invocazione.
        if await _complete_import_browser(campaign_id):
            return None
        if done_count >= cap:
            minutes = random.uniform(
                getattr(campaign, "scrape_break_minutes_min", 30) or 30,
                getattr(campaign, "scrape_break_minutes_max", 45) or 45,
            )
            emit_event(campaign_id, "scrape_break", f"Pausa risoluzione browser {int(minutes)} min")
            return max(60, int(minutes * 60))

        # Backstop iterazioni senza raggiungere il cap di risoluzioni reali (lista
        # skip-heavy: not_found/error). Defer breve per smaltire subito il resto.
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
