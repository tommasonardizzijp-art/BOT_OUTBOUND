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
from datetime import datetime
from types import SimpleNamespace

from loguru import logger

from app.config import settings
from app.models.follower import FollowerStatus

# App-id pubblico del web di Instagram (usato dal suo stesso JS per web_profile_info).
WEB_APP_ID = "936619743392459"
_WEB_PROFILE_PATH = "/api/v1/users/web_profile_info/"


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


async def run_pause_browser_activity(campaign, db, account_id: str, username: str | None = None) -> int:
    """Durante la pausa lunga della Fase Bio: UNA sessione browser coerente sull'account
    appena usato = scroll organico (warm-up) + eventuale BLOCCO di profili scrapati.
    Ritorna i secondi totali spesi (0 se tutto disabilitato o fallito). Difensivo: non
    solleva mai — il chiamante scala questo tempo dal defer della pausa.

    Coerenza IP: BrowserSession esce dallo STESSO account.proxy dell'API. Nessuna
    concorrenza: gira mentre il job API e' in procinto di defer (job singolo seriale).
    Un solo login/apertura sessione per pausa (non per profilo) = comportamento umano.
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
