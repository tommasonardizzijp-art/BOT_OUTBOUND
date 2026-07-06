"""Fase Bio via BROWSER (Patchright) — alternativa credibile all'API mobile.

Perche' esiste: instagrapi chiama gli endpoint privati mobile con una sessione
web-born su device sintetico -> firma da automazione (vedi memory
[[botoutbound-checkpoint-pattern-api]]). Aprire il profilo in un browser vero e'
molto piu' tollerato: la richiesta ai dati profilo viaggia dentro una sessione
Chromium legittima (cookie reali, header, TLS, referer della navigazione).

Come funziona (correzione al modello "nessuna chiamata API"): Instagram web e' una
SPA React. Navigando su /<username>/ i dati profilo arrivano comunque da una
chiamata interna `web_profile_info` (o embedded nell'HTML) — la fa il JS di IG, non
noi. Noi la INTERCETTIAMO passivamente (nessuna chiamata extra); solo se non la
cogliamo entro il timeout facciamo un fetch in-page (dentro il contesto della
pagina, con x-ig-app-id come l'app). Niente API instagrapi -> NON consuma il cap
scrape_daily_limit.

Anti-divergenza: i campi del JSON web sono mappati su uno shim con gli STESSI nomi
attributo dell'oggetto instagrapi, poi passati allo stesso `extract_contacts` e
scritti sugli stessi campi Follower + `upsert_lead` di `fetch_and_store_bio`.
"""
import asyncio
import json as _json
import random
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import arq
from loguru import logger
from sqlalchemy import update

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.follower import FollowerStatus
from app.services.bot_state_service import is_halted
from app.services.scrape_bios import bio_should_continue, pick_session_cap
from app.services.work_enqueue import arq_redis_settings, ARQ_MAIN_QUEUE
from app.browser.context_manager import BrowserSession

# App-id pubblico del web di Instagram (usato dal suo stesso JS per web_profile_info).
WEB_APP_ID = "936619743392459"
_WEB_PROFILE_PATH = "/api/v1/users/web_profile_info/"

# Backstop iterazioni per mini-sessione: `cap` conta solo gli outcome 'done'. Un pool
# di pending prevalentemente private/not_found/error potrebbe non raggiungere mai
# `cap` pur richiedendo moltissime iterazioni (ognuna con un human_profile_pause di
# 5-10s+), rischiando di sforare job_timeout. Limite totale iterazioni (done+skip) =
# cap * questo moltiplicatore.
MAX_SESSION_ITERATIONS_MULTIPLIER = 5


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def web_user_to_shim(user: dict) -> SimpleNamespace:
    """Mappa il dict `data.user` di web_profile_info su un oggetto con gli stessi
    nomi-attributo che `extract_contacts`/lo storage si aspettano dall'oggetto
    instagrapi. Pura e testabile: nessun IO. Robusta a chiavi mancanti."""
    user = user or {}
    followed_by = user.get("edge_followed_by") or {}
    follows = user.get("edge_follow") or {}

    # bio_links nel web arriva come lista di {url,title,link_type,lynx_url}. Lo
    # normalizziamo a {url,title} come si aspetta _bio_links_from (che gestisce dict).
    raw_bio_links = user.get("bio_links") or []
    bio_links = []
    for bl in raw_bio_links:
        if isinstance(bl, dict) and bl.get("url"):
            bio_links.append({"url": bl.get("url"), "title": bl.get("title") or bl.get("link_type")})

    return SimpleNamespace(
        pk=user.get("id"),
        username=user.get("username"),
        full_name=user.get("full_name"),
        biography=user.get("biography") or "",
        is_verified=bool(user.get("is_verified", False)),
        is_private=bool(user.get("is_private", False)),
        follower_count=_to_int(followed_by.get("count")),
        following_count=_to_int(follows.get("count")),
        external_url=user.get("external_url"),
        # Campi business: il web li espone come business_email/business_phone_number.
        public_email=user.get("business_email"),
        public_phone_number=user.get("business_phone_number"),
        contact_phone_number=user.get("business_phone_number"),
        public_phone_country_code=None,  # il web li da' gia' uniti nel numero business
        bio_links=bio_links,
        whatsapp_number=None,  # non esposto sul web; il regex sulla bio cattura wa.me
    )


async def _capture_web_profile_info(raw_page, username: str, timeout_s: float = 8.0) -> dict | None:
    """Naviga al profilo e cattura il JSON di web_profile_info.

    Strategia (dalla piu' "umana" alla piu' esplicita):
      1. Registra un listener sulle response e naviga: se il JS di IG spara
         web_profile_info lo intercettiamo passivamente (nessuna chiamata extra).
      2. Fallback: se entro timeout non l'abbiamo colto (dati SSR nell'HTML o
         endpoint GraphQL diverso), facciamo un fetch IN-PAGE con x-ig-app-id —
         gira nel contesto della pagina, con i cookie della sessione reale.

    Ritorna il dict `data.user` oppure None. Non solleva su errori di parsing.
    """
    captured: dict = {}

    async def _on_response(resp):
        try:
            if _WEB_PROFILE_PATH in resp.url and resp.status == 200:
                body = await resp.json()
                u = (((body or {}).get("data") or {}).get("user"))
                if u:
                    captured["user"] = u
        except Exception:
            pass  # response non-JSON o gia' consumata: ignora

    raw_page.on("response", _on_response)
    try:
        url = f"https://www.instagram.com/{username}/"
        await raw_page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # Attendi l'intercettazione passiva (polling breve).
        waited = 0.0
        while waited < timeout_s and "user" not in captured:
            await asyncio.sleep(0.4)
            waited += 0.4

        if "user" in captured:
            return captured["user"]

        # Fallback: fetch in-page (stessa sessione/cookie, header come l'app IG).
        try:
            result = await raw_page.evaluate(
                """async (args) => {
                    const [username, appId] = args;
                    const r = await fetch(
                        `/api/v1/users/web_profile_info/?username=${encodeURIComponent(username)}`,
                        { headers: { 'x-ig-app-id': appId }, credentials: 'include' }
                    );
                    if (!r.ok) return { __status: r.status };
                    return await r.json();
                }""",
                [username, WEB_APP_ID],
            )
            if isinstance(result, dict):
                if result.get("__status"):
                    logger.warning(f"[BioBrowser] @{username}: web_profile_info fetch HTTP {result['__status']}")
                    return {"__status": result["__status"]}
                u = (((result or {}).get("data") or {}).get("user"))
                if u:
                    return u
        except Exception as e:
            logger.warning(f"[BioBrowser] @{username}: fetch in-page fallito ({type(e).__name__}: {e})")
        return None
    finally:
        try:
            raw_page.remove_listener("response", _on_response)
        except Exception:
            pass


async def fetch_and_store_bio_browser(follower, campaign, db, browser_session) -> tuple[str, Exception | None]:
    """Come `fetch_and_store_bio` ma via browser. Scrive gli STESSI campi Follower +
    upsert_lead. NON consuma il cap API (nessun user_info_v1).

    Ritorna (outcome, err):
      'done' | 'private' | 'not_found' | 'soft_block' | 'network' | 'error'
    """
    from app.utils.contact_extract import extract_contacts
    from app.services.scraper import upsert_lead

    username = follower.username
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
        # Nessun dato: profilo inesistente o parsing a vuoto. Skip non fatale.
        return "not_found", None
    if isinstance(user, dict) and user.get("__status"):
        st = user["__status"]
        # 429/401/403 dal web = soft-block/rate: il chiamante rallenta o pausa.
        if st in (429, 401, 403):
            return "soft_block", Exception(f"web_profile_info HTTP {st}")
        return "error", Exception(f"web_profile_info HTTP {st}")

    shim = web_user_to_shim(user)
    contacts = extract_contacts(shim)

    # Aggiorna full_name se il web lo espone e in DB manca (la Fase Lista a volte no).
    if shim.full_name and not follower.full_name:
        follower.full_name = shim.full_name
    follower.biography = shim.biography or None
    follower.is_verified = bool(shim.is_verified)
    follower.follower_count = shim.follower_count
    follower.following_count = shim.following_count
    ext = shim.external_url
    follower.external_url = contacts.external_url or (str(ext) if ext else None)
    follower.phone = contacts.phone
    follower.email = contacts.email
    follower.whatsapp = contacts.whatsapp
    follower.bio_links = _json.dumps(contacts.bio_links) if contacts.bio_links else None
    follower.contact_source = _json.dumps(contacts.sources) if contacts.sources else None
    follower.status = FollowerStatus.bio_scraped
    follower.locked_by_account_id = None   # C2: libera il claim atomico (Task 4)
    follower.locked_at = None
    await db.commit()

    await upsert_lead(
        db,
        ig_user_id=follower.ig_user_id,
        username=follower.username,
        full_name=follower.full_name,
        biography=follower.biography,
        contacts=contacts,
        campaign=campaign,
        account=None,  # via browser: nessun account API attribuibile alla lookup
    )

    logger.info(f"[BioBrowser] @{username} bio via browser (no cap API)")
    return "done", None


async def human_profile_pause() -> None:
    """Pausa tra un profilo e l'altro: 5-10s + pausa breve occasionale (l'utente
    che si ferma a guardare). Ritmo credibile a detta dell'operatore."""
    await asyncio.sleep(random.uniform(5.0, 10.0))
    if random.random() < 0.12:
        extra = random.uniform(15.0, 45.0)
        logger.debug(f"[BioBrowser] pausa breve {extra:.0f}s (distrazione)")
        await asyncio.sleep(extra)


async def maybe_micro_scroll(session, *, rng=None) -> bool:
    """Scroll leggero sul profilo aperto, ~bio_browser_scroll_ratio dei profili,
    per 4-5s. Simula lo sguardo umano; non su tutti (la costanza è una firma).
    Difensivo: non solleva. Ritorna True se ha scrollato."""
    r = rng or random
    if r.random() >= settings.bio_browser_scroll_ratio:
        return False
    try:
        raw_page = await session.page._get_page()
        dur = r.uniform(settings.bio_browser_scroll_min_s, settings.bio_browser_scroll_max_s)
        steps = max(1, int(dur))
        for _ in range(steps):
            await raw_page.evaluate("window.scrollBy({top: 300, behavior: 'smooth'})")
            await asyncio.sleep(1.0)
        return True
    except Exception as e:
        logger.debug(f"[BioBrowser] micro-scroll saltato ({type(e).__name__}: {e})")
        return False


async def claim_next_pending(db, campaign_id: str, account_id: str):
    """Claima atomicamente un Follower pending non lockato per questo account.
    Rilascia prima gli stale lock della campagna (sessioni morte). Ritorna il
    Follower claimato o None. Optimistic lock: safe con più account paralleli
    (SQLite WAL / Postgres). Stesso schema del claim DM in campaign_orchestrator.
    """
    from sqlalchemy import select
    from app.models.follower import Follower, FollowerStatus
    from app.services.campaign_orchestrator import LOCK_TIMEOUT_MINUTES

    stale_cutoff = datetime.utcnow() - timedelta(minutes=LOCK_TIMEOUT_MINUTES)
    await db.execute(
        update(Follower).where(
            Follower.campaign_id == campaign_id,
            Follower.status == FollowerStatus.pending,
            Follower.locked_by_account_id.isnot(None),
            Follower.locked_at < stale_cutoff,
        ).values(locked_by_account_id=None, locked_at=None)
    )
    await db.commit()

    for _ in range(25):  # ritenta se un altro account claima tra SELECT e UPDATE
        follower = (await db.execute(
            select(Follower).where(
                Follower.campaign_id == campaign_id,
                Follower.status == FollowerStatus.pending,
                Follower.locked_by_account_id.is_(None),
            ).limit(1)
        )).scalar_one_or_none()
        if follower is None:
            return None
        claim = await db.execute(
            update(Follower).where(
                Follower.id == follower.id,
                Follower.locked_by_account_id.is_(None),
            ).values(locked_by_account_id=account_id, locked_at=datetime.utcnow())
        )
        await db.commit()
        if claim.rowcount == 1:
            await db.refresh(follower)
            return follower
    return None


async def _maybe_complete_browser_bio(campaign_id: str) -> bool:
    """Specchio del completamento lato API (`scrape_bios.py:326-329`): quando il pool
    globale di pending e' esaurito OPPURE il target e' raggiunto, porta la campagna
    a 'ready' ed emette 'scrape_complete'. Senza questo il path browser non ha MAI
    un modo di uscire da 'scraping' (era il bug: la fase restava bloccata per sempre).

    Conta i pending sull'INTERA campagna (non per-account): con piu' account paralleli
    e' l'ultimo che svuota il pool globale (o fa scattare il target) a completare;
    finche' un altro account tiene ancora righe pending (anche locked) la campagna
    resta aperta, e completera' quando quell'account a sua volta esaurira' il pool.

    L'UPDATE e' atomico e condizionato (`status IN (scraping, scraping_break)`) con
    controllo su `rowcount`: se piu' account/task arrivano qui in corsa, SOLO uno
    vince la transizione e quindi SOLO uno emette l'evento (no doppio scrape_complete).

    Non tocca `scrape_completed_at`/`scrape_outcome` (di competenza della Fase Lista,
    coerente col path API che a sua volta non li tocca in questo punto).
    """
    from sqlalchemy import select, func
    from app.models.campaign import Campaign, CampaignStatus
    from app.models.follower import Follower, FollowerStatus
    from app.utils.events import emit as emit_event

    async with AsyncSessionLocal() as db:
        campaign = (await db.execute(
            select(Campaign).where(Campaign.id == campaign_id)
        )).scalar_one_or_none()
        if campaign is None:
            return False

        pending_count = await db.scalar(
            select(func.count()).select_from(Follower).where(
                Follower.campaign_id == campaign_id,
                Follower.status == FollowerStatus.pending,
            )
        ) or 0
        bio_done = await db.scalar(
            select(func.count()).select_from(Follower).where(
                Follower.campaign_id == campaign_id,
                Follower.status == FollowerStatus.bio_scraped,
            )
        ) or 0

        target_reached = campaign.bio_target is not None and bio_done >= campaign.bio_target
        if pending_count > 0 and not target_reached:
            return False  # altro account sta ancora smaltendo il pool: non e' finita

        result = await db.execute(
            update(Campaign).where(
                Campaign.id == campaign_id,
                Campaign.status.in_([CampaignStatus.scraping, CampaignStatus.scraping_break]),
            ).values(status=CampaignStatus.ready, updated_at=datetime.utcnow())
        )
        await db.commit()
        if result.rowcount == 1:
            emit_event(campaign_id, "scrape_complete", f"Fase Bio (browser) completata: {bio_done} bio")
            return True
        return False  # gia' transitata da un'altra race (o non era in scraping/*_break)


async def scrape_bios_browser_session(campaign_id: str, account_id: str) -> int | None:
    """Una mini-sessione browser per UN account: apre, scrapa fino a un cap di
    profili claimati (pool disgiunto via claim_next_pending), chiude. Ritorna i
    secondi di defer per la pausa lunga anti-block, o None se non c'è più lavoro.
    Job corto: mai oltre job_timeout. Difensiva sui singoli profili."""
    from sqlalchemy import select, func
    from app.models.campaign import Campaign, CampaignStatus
    from app.models.follower import Follower, FollowerStatus
    from app.utils.events import emit as emit_event

    async with AsyncSessionLocal() as db:
        campaign = (await db.execute(
            select(Campaign).where(Campaign.id == campaign_id)
        )).scalar_one_or_none()
        if campaign is None or campaign.status not in (
            CampaignStatus.scraping, CampaignStatus.scraping_break
        ):
            return None
        if await is_halted(db):
            return None

        done = await db.scalar(
            select(func.count()).select_from(Follower).where(
                Follower.campaign_id == campaign_id,
                Follower.status == FollowerStatus.bio_scraped))
        if not bio_should_continue(campaign.bio_target, done or 0):
            await _maybe_complete_browser_bio(campaign_id)
            return None

    cap = pick_session_cap(settings.bio_browser_session_cap_min, settings.bio_browser_session_cap_max)
    max_iterations = cap * MAX_SESSION_ITERATIONS_MULTIPLIER
    done_count = 0
    iterations = 0
    target_reached = False
    session = None
    try:
        session = BrowserSession(account_id, headless=settings.bio_browser_headless)
        await session.open()
        await session.page.ensure_logged_in(account_id)

        while done_count < cap and iterations < max_iterations:
            async with AsyncSessionLocal() as db:
                if await is_halted(db):
                    return None
                campaign = (await db.execute(
                    select(Campaign).where(Campaign.id == campaign_id)
                )).scalar_one_or_none()
                if campaign is None or campaign.status not in (
                    CampaignStatus.scraping, CampaignStatus.scraping_break
                ):
                    return None

                # Re-check del target globale ad ogni iterazione: con piu' sessioni
                # per-account in parallelo il pre-check fatto prima del loop puo'
                # essere superato nel frattempo (evita overshoot su bio_target).
                bio_done_total = await db.scalar(
                    select(func.count()).select_from(Follower).where(
                        Follower.campaign_id == campaign_id,
                        Follower.status == FollowerStatus.bio_scraped))
                if not bio_should_continue(campaign.bio_target, bio_done_total or 0):
                    target_reached = True
                    break

                follower = await claim_next_pending(db, campaign_id, account_id)
                if follower is None:
                    await _maybe_complete_browser_bio(campaign_id)
                    return None  # pool globale esaurito
                try:
                    outcome, err = await fetch_and_store_bio_browser(follower, campaign, db, session)
                except Exception as e:
                    logger.warning(f"[BioBrowser] @{follower.username} errore inatteso ({e}) — skip")
                    outcome, err = "error", e

                iterations += 1
                if outcome == "done":
                    done_count += 1
                    emit_event(campaign_id, "scrape_progress", f"@{follower.username} bio via browser")
                elif outcome in ("not_found", "private", "error"):
                    follower.status = FollowerStatus.skipped
                    follower.skip_reason = f"browser_{outcome}"
                    follower.locked_by_account_id = None
                    follower.locked_at = None
                    follower.updated_at = datetime.utcnow()
                    await db.commit()
                elif outcome == "soft_block":
                    # Non bruciare i pending: rilascia il claim e fai backoff invece di
                    # `return None` (che lasciava l'account silenziosamente sideline: il
                    # task ARQ finiva senza Retry e non si ri-accodava piu' da solo per
                    # questa run). Ritornando un defer il worker fa Retry(defer=...) e
                    # l'account riprova piu' tardi, senza essere abbandonato.
                    # NB: un guard globale "pausa campagna dopo N soft-block consecutivi
                    # cross-account" e' rimandato (richiederebbe stato condiviso in
                    # Redis/DB oltre il lock per-follower gia' presente); l'operatore
                    # mantiene comunque la pausa manuale dal control plane, e ogni
                    # soft-block resta visibile via l'evento qui sotto.
                    follower.locked_by_account_id = None
                    follower.locked_at = None
                    await db.commit()
                    logger.warning(f"[BioBrowser] soft-block su @{follower.username}: {err} — backoff")
                    emit_event(
                        campaign_id, "scrape_progress",
                        f"@{follower.username}: soft-block (429) — backoff", level="warn",
                    )
                    return random.randint(900, 1800)  # 15-30 min
                elif outcome == "network":
                    # Stessa logica del soft_block: rilascia il claim, retry breve
                    # invece di sideline silenzioso.
                    follower.locked_by_account_id = None
                    follower.locked_at = None
                    await db.commit()
                    logger.warning(f"[BioBrowser] errore rete su @{follower.username}: {err} — retry breve")
                    return 180  # 3 min
                else:
                    # Outcome inatteso (fetch_and_store_bio_browser cambiato senza
                    # aggiornare qui): non stranare il follower (pending+locked per
                    # sempre). Skip difensivo, libera il lock, conta come iterazione.
                    follower.status = FollowerStatus.skipped
                    follower.skip_reason = "browser_unknown"
                    follower.locked_by_account_id = None
                    follower.locked_at = None
                    follower.updated_at = datetime.utcnow()
                    await db.commit()
                    logger.warning(f"[BioBrowser] @{follower.username} outcome inatteso '{outcome}' — skip difensivo")

            await maybe_micro_scroll(session)
            await human_profile_pause()

        if target_reached:
            await _maybe_complete_browser_bio(campaign_id)
            return None

        if done_count >= cap:
            # cap raggiunto → pausa lunga anti-block via defer
            minutes = random.uniform(
                getattr(campaign, "scrape_break_minutes_min", 30) or 30,
                getattr(campaign, "scrape_break_minutes_max", 45) or 45,
            )
            emit_event(campaign_id, "scrape_break", f"Pausa bio browser {int(minutes)} min")
            return max(60, int(minutes * 60))

        # Backstop iterazioni raggiunto senza completare il cap di bio reali: pool
        # skip-heavy (private/not_found/error). Defer BREVE (non la pausa lunga
        # anti-block) cosi' la prossima mini-sessione riprende subito a smaltire
        # il pool invece di aspettare 30-45 min senza motivo.
        logger.info(
            f"[BioBrowser] backstop iterazioni ({iterations}/{max_iterations}) "
            f"raggiunto con solo {done_count}/{cap} bio reali — pool skip-heavy, defer breve"
        )
        return 60
    except Exception as e:
        logger.warning(f"[BioBrowser] mini-sessione @{account_id[:8]} fallita ({type(e).__name__}: {e})")
        # errore d'apertura/login: breve retry via defer, non perde i pending
        return 300
    finally:
        if session is not None:
            try:
                await session.close()
            except Exception:
                pass


async def _scrape_batch(campaign, db, browser_session, count: int) -> int:
    """Scrapa fino a `count` follower pending via la sessione browser gia' aperta,
    a ritmo umano. Ritorna quante bio estratte. Difensivo: non solleva."""
    from sqlalchemy import select
    from app.models.follower import Follower

    done = 0
    for _ in range(count):
        follower = (await db.execute(
            select(Follower).where(
                Follower.campaign_id == campaign.id,
                Follower.status == FollowerStatus.pending,
            ).limit(1)
        )).scalar_one_or_none()
        if follower is None:
            break  # niente piu' pending

        try:
            outcome, err = await fetch_and_store_bio_browser(follower, campaign, db, browser_session)
        except Exception as e:
            logger.warning(f"[BioBrowser] batch: errore inatteso su @{follower.username} ({e}) — stop batch")
            break

        if outcome == "done":
            done += 1
        elif outcome in ("not_found", "private", "error"):
            # Skip benigno: marca skipped cosi' non ri-seleziona lo stesso pending
            # (limit(1) senza ORDER BY ritornerebbe lo stesso -> loop).
            follower.status = FollowerStatus.skipped
            follower.skip_reason = f"browser_{outcome}"
            follower.updated_at = datetime.utcnow()
            await db.commit()
        elif outcome in ("soft_block", "network"):
            # 429/soft-block o rete giu': fermati, NON bruciare i pending buoni
            # (restano pending per l'API alla ripresa).
            logger.warning(f"[BioBrowser] batch stop ({outcome}) su @{follower.username}: {err}")
            break

        await human_profile_pause()

    logger.info(f"[BioBrowser] batch pausa: {done} bio estratte")
    return done


async def _scraping_accounts_of_campaign(campaign_id: str):
    """(account_id, username) degli account scraping/both attivi della campagna."""
    from app.database import AsyncSessionLocal
    from sqlalchemy import select
    from app.models.campaign_account import CampaignAccount
    from app.models.account import InstagramAccount, AccountStatus
    from app.utils.roles import SCRAPE_ROLES
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(InstagramAccount.id, InstagramAccount.username)
            .join(CampaignAccount, CampaignAccount.account_id == InstagramAccount.id)
            .where(
                CampaignAccount.campaign_id == campaign_id,
                CampaignAccount.is_active == True,  # noqa: E712
                CampaignAccount.role.in_(SCRAPE_ROLES),
                InstagramAccount.status == AccountStatus.active,
            )
        )).all()
    return [(r[0], r[1]) for r in rows]


def browser_bio_job_id(campaign_id: str, account_id: str) -> str:
    return f"biobrowser:{campaign_id}:{account_id}"


def browser_bio_redis_keys(campaign_id: str, account_id: str) -> tuple[str, str, str]:
    """Mirror di `dm_worker_redis_keys` (work_enqueue.py) per i job Bio browser."""
    job_id = browser_bio_job_id(campaign_id, account_id)
    return (
        f"arq:job:{job_id}",
        f"arq:retry:{job_id}",
        f"arq:in-progress:{job_id}",
    )


async def enqueue_browser_bio_workers(campaign_id: str) -> int:
    """Fan-out: un task ARQ per account scraping, con stagger crescente via
    _defer_by (partenze differite) e _job_id deterministico (dedup, come i DM).

    Stessa disciplina di `_reenqueue_phase` in work_enqueue.py: un job
    parcheggiato in `Retry(defer=...)` (pausa lunga anti-block) resta dedup-ato
    da ARQ sul suo `_job_id` finche' le chiavi `arq:job`/`arq:retry` non
    vengono cancellate. Senza questo, uno stop→resume durante la pausa lunga
    sarebbe un no-op silenzioso (il job non si risveglia mai). Se il job e'
    gia' 'in-progress' (worker vivo in questo momento) NON lo cancelliamo ne'
    lo ri-accodiamo, per non far partire un secondo worker concorrente sullo
    stesso account.

    Ritorna il numero di account ORA schedulati: sia quelli appena accodati
    sia quelli gia' in esecuzione (in-progress), cosi' l'evento "N account in
    parallelo" del chiamante resta veritiero."""
    accounts = await _scraping_accounts_of_campaign(campaign_id)
    if not accounts:
        return 0
    redis = await arq.create_pool(arq_redis_settings())
    lo = min(settings.bio_browser_stagger_min_s, settings.bio_browser_stagger_max_s)
    hi = max(settings.bio_browser_stagger_min_s, settings.bio_browser_stagger_max_s)
    n = 0
    try:
        for idx, (account_id, _username) in enumerate(accounts):
            job_id = browser_bio_job_id(campaign_id, account_id)
            job_key, retry_key, in_progress_key = browser_bio_redis_keys(campaign_id, account_id)
            if await redis.exists(in_progress_key):
                logger.info(f"[BioBrowser] {job_id} gia' in esecuzione — skip enqueue duplicato")
                n += 1
                continue
            await redis.delete(job_key, retry_key)
            defer = 0 if idx == 0 else int(random.uniform(lo, hi) * idx)
            await redis.enqueue_job(
                "browser_bio_account_task",
                campaign_id,
                account_id,
                _job_id=job_id,
                _defer_by=defer,
                _queue_name=ARQ_MAIN_QUEUE,
            )
            n += 1
    finally:
        await redis.aclose()  # non lasciare la connessione ARQ aperta (come le helper in work_enqueue)
    return n


async def run_pause_browser_activity(campaign_id: str, account_id: str, username: str | None = None) -> int:
    """Durante la pausa lunga della Fase Bio: UNA sessione browser coerente su UN account
    = scroll organico (warm-up) + eventuale BLOCCO di profili scrapati. Ritorna i secondi
    spesi (0 se disabilitato/fallito). Difensivo: non solleva mai.

    Apre la PROPRIA db session (via AsyncSessionLocal) SOLO per il batch, cosi' e'
    sicura chiamata in parallelo su piu' account (le sessioni SQLAlchemy async non sono
    concorrenti-safe). Coerenza IP: BrowserSession esce dallo STESSO account.proxy dell'API.
    Un solo login/apertura sessione per account (non per profilo) = comportamento umano.
    """
    do_scroll = settings.warmup_browse_enabled
    do_batch = settings.bio_browser_batch_enabled
    if not do_scroll and not do_batch:
        return 0

    tag = f"@{username}" if username else account_id[:8] + "…"
    start = time.monotonic()
    session = None
    try:
        from app.browser.context_manager import BrowserSession
        session = BrowserSession(account_id, headless=settings.warmup_browse_headless)
        await session.open()
        await session.page.ensure_logged_in(account_id)

        # 1) Scroll organico (warm-up): feed scroll, post, like ~35%.
        if do_scroll:
            scroll_s = int(random.uniform(
                settings.warmup_browse_min_minutes, settings.warmup_browse_max_minutes
            ) * 60)
            logger.info(f"[BioBrowser] {tag}: scroll organico ~{scroll_s}s")
            await session.page.browse_feed(scroll_s)

        # 2) Blocco di profili scrapati nella STESSA sessione (piu' umano di 1 sporadico).
        if do_batch:
            lo = min(settings.bio_browser_batch_min, settings.bio_browser_batch_max)
            hi = max(settings.bio_browser_batch_min, settings.bio_browser_batch_max)
            n = random.randint(lo, hi)
            logger.info(f"[BioBrowser] {tag}: batch di {n} profili via browser")
            from app.database import AsyncSessionLocal
            from app.models.campaign import Campaign
            from sqlalchemy import select
            async with AsyncSessionLocal() as db:  # db propria = parallel-safe
                campaign = (await db.execute(
                    select(Campaign).where(Campaign.id == campaign_id)
                )).scalar_one_or_none()
                if campaign is not None:
                    await _scrape_batch(campaign, db, session, n)

        return int(time.monotonic() - start)
    except Exception as e:
        logger.warning(f"[BioBrowser] {tag}: attivita' in pausa fallita ({type(e).__name__}: {e}) — ingoiato")
        return int(time.monotonic() - start)
    finally:
        if session is not None:
            try:
                await session.close()
            except Exception as e:  # pragma: no cover
                logger.debug(f"[BioBrowser] {tag}: close fallita ({e})")


async def run_pause_browser_all_accounts(campaign_id: str) -> int:
    """Ogni account scraping della campagna fa la sua sessione scroll+batch in pausa.
    Parallelo con partenze SCAGLIONATE (offset random 1-3 min tra account, mai tutti
    nello stesso istante) e cap sui browser concorrenti (max_concurrent_browsers).
    Ritorna i secondi totali spesi (0 se disabilitato / nessun account). Difensivo."""
    if not (settings.warmup_browse_enabled or settings.bio_browser_batch_enabled):
        return 0
    accounts = await _scraping_accounts_of_campaign(campaign_id)
    if not accounts:
        return 0

    start = time.monotonic()
    sem = asyncio.Semaphore(max(1, settings.max_concurrent_browsers))

    async def _one(account_id, username, idx):
        # Stagger: parte dopo idx * (1-3 min), cosi' non tutti nello stesso istante.
        if idx:
            await asyncio.sleep(random.uniform(60.0, 180.0) * idx)
        async with sem:
            await run_pause_browser_activity(campaign_id, account_id, username)

    await asyncio.gather(
        *[_one(a, u, i) for i, (a, u) in enumerate(accounts)],
        return_exceptions=True,  # un account che fallisce non blocca gli altri
    )
    logger.info(f"[BioBrowser] pausa: sessioni browser completate su {len(accounts)} account")
    return int(time.monotonic() - start)
