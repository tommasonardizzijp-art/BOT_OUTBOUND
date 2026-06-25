"""
Manual Instagram login via visible Patchright browser.

Flow:
1. Opens a real Chromium browser (headed mode) to Instagram login page
2. User logs in manually — handles 2FA, challenges, captchas themselves
3. Bot detects login by polling for the `sessionid` cookie
4. Verifies the session via WEB navigation in the same browser context
   (no mobile API call — avoids UFAC challenge from web→mobile session jump)
5. Performs short organic browse to bake the session before close
6. Extracts cookies and builds instagrapi-compatible session dict
7. Returns the session dict (mobile API verification deferred to scraper)

This avoids the risky instagrapi automated login that triggers IP bans
AND the UFAC ("ufac_www_bloks") challenge triggered by calling mobile
private API endpoints immediately after a web login.
"""
import asyncio
import os
import random

from loguru import logger


# How long to wait for the user to complete login (seconds)
LOGIN_TIMEOUT = 300  # 5 minutes

# Cookies that matter for instagrapi's private API
ESSENTIAL_COOKIES = (
    "sessionid",    # Main session — required
    "csrftoken",    # CSRF token — required for POST requests
    "ds_user_id",   # Logged-in user's numeric IG ID
    "mid",          # Machine/browser ID
    "ig_did",       # Device ID
    "rur",          # Region routing
    "ig_nrcb",      # Notification read count
    "datr",         # Browser tracking
)


async def manual_browser_login(account_id: str, username: str, proxy_url: str | None = None) -> dict:
    """
    Opens a visible browser to instagram.com/accounts/login/.
    Waits for the user to complete login manually.
    Returns verified instagrapi-compatible settings dict.

    proxy_url MUST be resolved by the caller on the main event loop and passed in.
    This function runs inside a thread-private event loop (see manual_browser_login_sync),
    where touching the shared async DB pool — bound to the main loop — would raise
    "Future attached to a different loop". So no DB lookup happens here.

    Raises:
        TimeoutError: if login not completed within LOGIN_TIMEOUT seconds.
        RuntimeError: if Patchright is not installed or session verification fails.
    """
    try:
        from patchright.async_api import async_playwright
    except ImportError:
        raise RuntimeError(
            "Patchright non installato. Esegui: pip install patchright && patchright install chromium"
        )

    from app.config import settings
    from app.browser.context_manager import parse_proxy_url, _build_fingerprint_script
    from app.browser.fingerprint import get_fingerprint

    profile_dir = os.path.join(settings.browser_profiles_dir, account_id)
    os.makedirs(profile_dir, exist_ok=True)

    fingerprint = get_fingerprint(account_id)
    proxy_cfg = parse_proxy_url(proxy_url)
    if proxy_url and not proxy_cfg:
        raise RuntimeError(
            f"Proxy configurato per @{username} ma malformato: {proxy_url!r}. "
            "Correggi il campo proxy prima di rifare login."
        )

    logger.info(
        f"Avvio browser per login manuale di @{username} | "
        f"proxy={'attivo (' + proxy_cfg['server'] + ')' if proxy_cfg else 'NESSUNO — IP locale'}"
    )

    async with async_playwright() as pw:
        chromium_args = [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
        ]
        launch_kwargs = dict(
            user_data_dir=profile_dir,
            headless=False,
            viewport=fingerprint["viewport"],
            user_agent=fingerprint["user_agent"],
            locale=fingerprint["locale"],
            timezone_id=fingerprint["timezone_id"],
            args=chromium_args,
            ignore_default_args=["--enable-automation"],
        )
        if proxy_cfg:
            launch_kwargs["proxy"] = proxy_cfg
        else:
            # Force direct connection — see context_manager.py rationale
            chromium_args.append("--no-proxy-server")
        context = await pw.chromium.launch_persistent_context(**launch_kwargs)

        # Apply same fingerprint init script used by DM sender — consistency across sessions
        await context.add_init_script(_build_fingerprint_script(fingerprint))

        try:
            page = await context.new_page()

            # Navigate to Instagram login
            await page.goto(
                "https://www.instagram.com/accounts/login/",
                wait_until="domcontentloaded",
            )

            logger.info(
                f"Browser aperto per @{username}. "
                f"In attesa del login manuale (max {LOGIN_TIMEOUT // 60} min)..."
            )

            # Give the page time to load and possibly redirect if already logged in
            await asyncio.sleep(3)

            # ── Poll for the sessionid cookie ──
            session_cookies = None
            for tick in range(LOGIN_TIMEOUT):
                await asyncio.sleep(1)

                cookies = await context.cookies("https://www.instagram.com")
                cookie_dict = {c["name"]: c["value"] for c in cookies}

                if cookie_dict.get("sessionid"):
                    # If detected within the first 5 seconds, the profile might
                    # already have old cookies. Verify the page has actually
                    # navigated away from the login page (real logged-in state).
                    if tick < 5:
                        current_url = page.url
                        if "/accounts/login" in current_url:
                            # Still on login page → cookie is stale, keep waiting
                            continue

                    logger.info(f"Login rilevato per @{username}! Estrazione cookie...")
                    session_cookies = cookie_dict
                    break

            if not session_cookies:
                raise TimeoutError(
                    "Login non completato entro 5 minuti. Clicca di nuovo 'Login Browser' per riprovare."
                )

            # Wait for all cookies to settle (some are set async after redirect)
            await asyncio.sleep(3)

            # ── Web-side session verification (NO mobile API call) ──
            # Calling instagrapi mobile API immediately after web login triggers
            # UFAC challenge (web→mobile session jump). Verify via web navigation
            # in the SAME browser context that just logged in.
            try:
                await page.goto(
                    "https://www.instagram.com/accounts/edit/",
                    wait_until="domcontentloaded",
                    timeout=15000,
                )
                await asyncio.sleep(random.uniform(2.5, 4.0))
                current_url = page.url
                if "/accounts/login" in current_url or "/challenge" in current_url:
                    raise RuntimeError(
                        f"Sessione web non valida dopo login (redirect → {current_url}). "
                        "Cookie non sono autenticati. Riprova login."
                    )
                logger.info(f"Sessione web verificata per @{username} (URL: {current_url})")
            except Exception as e:
                if isinstance(e, RuntimeError):
                    raise
                logger.warning(
                    f"Verifica web fallita per @{username}: {type(e).__name__}: {e}. "
                    "Proseguo comunque con estrazione cookie."
                )

            # ── Organic browse to bake session before close ──
            # Visit feed + small scroll. Makes the session look like a real
            # human login session, not a scripted credential capture.
            try:
                await page.goto(
                    "https://www.instagram.com/",
                    wait_until="domcontentloaded",
                    timeout=15000,
                )
                await asyncio.sleep(random.uniform(4.0, 7.0))
                # Light scroll
                for _ in range(random.randint(2, 4)):
                    await page.mouse.wheel(0, random.randint(300, 700))
                    await asyncio.sleep(random.uniform(1.5, 3.5))
                await asyncio.sleep(random.uniform(2.0, 4.0))
            except Exception as e:
                logger.debug(f"Organic browse skipped: {type(e).__name__}: {e}")

            # Re-extract cookies after web activity (some refresh on activity)
            cookies = await context.cookies("https://www.instagram.com")
            session_cookies = {c["name"]: c["value"] for c in cookies}

        finally:
            # Always close the browser, even on error
            await context.close()
            logger.debug("Browser chiuso.")

    # ── Extract only essential cookies ──
    essential = {}
    for key in ESSENTIAL_COOKIES:
        if key in session_cookies:
            essential[key] = session_cookies[key]

    if "sessionid" not in essential:
        raise RuntimeError("Cookie sessionid non trovato. Login non riuscito.")

    logger.info(
        f"Cookie estratti per @{username}: "
        f"{', '.join(essential.keys())} ({len(essential)} cookie)"
    )

    # ── Build instagrapi-compatible session dict ──
    # Mobile API verification is intentionally SKIPPED here. Calling mobile
    # private API right after web login triggers UFAC challenge
    # ("ufac_www_bloks") because Instagram sees an impossible web→mobile
    # session jump within seconds. Session is already verified web-side
    # above (goto /accounts/edit/). First mobile call deferred to scraper,
    # which gives the session time to "bake".
    return await _build_session(essential, username)


async def _build_session(cookies: dict, username: str) -> dict:
    """
    Build an instagrapi-compatible settings dict from web cookies.

    Uses instagrapi's OWN default device settings (mobile UA, UUIDs, device info)
    combined with the essential cookies from the browser login. Does NOT call
    any mobile API — verification happens web-side in the browser context
    BEFORE this function is called.
    """
    try:
        from instagrapi import Client
    except ImportError:
        raise RuntimeError(
            "instagrapi non installato. Esegui: pip install instagrapi"
        )

    def _do_build():
        client = Client()
        default_settings = client.get_settings()
        default_settings["cookies"] = cookies
        default_settings["authorization_data"] = {
            "ds_user_id": cookies.get("ds_user_id", ""),
            "sessionid": cookies.get("sessionid", ""),
            # Mirror instagrapi's canonical login_by_sessionid: this flag tells
            # Instagram to authenticate the private mobile API via the Bearer
            # header. Without it, header-auth endpoints like direct_v2/inbox
            # return 404 ("Endpoint does not exist") — breaks DM inbox scraping.
            "should_use_header_over_cookies": True,
        }
        client.set_settings(default_settings)

        if not client.user_id:
            raise RuntimeError(
                "Cookie ds_user_id mancante o vuoto. "
                "Login web non ha prodotto sessione valida."
            )

        logger.info(
            f"Sessione costruita per @{username} (user_id={client.user_id}). "
            "Verifica mobile API rinviata al primo uso scraper."
        )
        return client.get_settings()

    return await asyncio.to_thread(_do_build)


async def manual_browse_session(account_id: str, username: str, max_minutes: int = 60, proxy_url: str | None = None) -> dict:
    """
    Open Instagram in a real browser using account profile + proxy + fingerprint
    of `account_id`. User browses normally (scroll feed, like posts, view stories).
    Waits until the user closes the browser (or `max_minutes` elapse).

    Purpose: warm-up dormant accounts and accumulate organic activity signals
    on the SAME profile/proxy/fingerprint that the bot will later use for DMs.

    proxy_url MUST be resolved by the caller on the main loop and passed in — same
    thread-private-loop reason as manual_browser_login.

    Returns: {"duration_seconds": int, "closed_by": "user" | "timeout"}
    """
    from app.browser.context_manager import get_browser_context
    import time

    started = time.time()
    closed_by = "timeout"
    deadline = started + max_minutes * 60

    logger.info(f"Avvio sessione browse manuale per @{username} (max {max_minutes}min)")

    async with get_browser_context(account_id, headless=False, proxy_url=proxy_url) as context:
        page = await context.new_page()
        await page.goto("https://www.instagram.com/", wait_until="domcontentloaded")

        # Poll for browser closure — Patchright fires `close` event on context when
        # the user closes the last window. Detect via context.pages becoming empty.
        while time.time() < deadline:
            await asyncio.sleep(2)
            try:
                if not context.pages:
                    closed_by = "user"
                    break
            except Exception:
                # Context already closed
                closed_by = "user"
                break

    duration = int(time.time() - started)
    logger.info(f"Sessione browse @{username} chiusa: {duration}s ({closed_by})")
    return {"duration_seconds": duration, "closed_by": closed_by}


def manual_browse_session_sync(account_id: str, username: str, max_minutes: int = 60, proxy_url: str | None = None) -> dict:
    """Synchronous wrapper for manual_browse_session — same pattern as manual_browser_login_sync.
    proxy_url must be resolved on the main loop by the caller (see that wrapper)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(manual_browse_session(account_id, username, max_minutes, proxy_url))
    finally:
        loop.close()


def manual_browser_login_sync(account_id: str, username: str, proxy_url: str | None = None) -> dict:
    """
    Synchronous wrapper for manual_browser_login.

    Runs the async browser login in its OWN event loop (new thread).
    This is needed because FastAPI/uvicorn already runs its own asyncio loop,
    and Patchright's async_playwright() can conflict with it.

    proxy_url MUST be resolved on the MAIN loop by the caller and passed in. The
    shared async DB pool (app.database.engine) is bound to the main loop; querying
    it from this thread-private loop raises "Future attached to a different loop".

    Called from the endpoint via: await asyncio.to_thread(manual_browser_login_sync, ...)
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(manual_browser_login(account_id, username, proxy_url))
    finally:
        loop.close()
