"""
Instagram Page Object Model.

Wraps all browser interactions with Instagram using Patchright.
Uses human-like timing and interaction patterns.

Each method corresponds to a distinct Instagram action.
"""
import asyncio
import math
import random
from loguru import logger
from app.utils.timing import (
    pre_dm_browse_seconds,
    extended_pre_dm_browse_seconds,
    post_dm_dwell_seconds,
)
from typing import Callable, Awaitable, Optional
from app.utils.exceptions import (
    DMSendError, DMRestrictedError, AccountBannedError, AccountChallengeError,
    DMAbortedBeforeSendError,
)


# Adjacent QWERTY keys — used to generate realistic single-character typos
_QWERTY_ADJACENT: dict[str, str] = {
    'q': 'wa',   'w': 'qes',  'e': 'wrd',  'r': 'etf',  't': 'ryg',
    'y': 'tuh',  'u': 'yij',  'i': 'uok',  'o': 'ipl',  'p': 'ol',
    'a': 'qsz',  's': 'awdz', 'd': 'sefc', 'f': 'drgv', 'g': 'fthb',
    'h': 'gyun', 'j': 'huim', 'k': 'jiol', 'l': 'kop',
    'z': 'asx',  'x': 'zdc',  'c': 'xfv',  'v': 'cgb',  'b': 'vhn',
    'n': 'bhm',  'm': 'nj',
}


def _typo_char(char: str) -> str | None:
    """Return a plausible adjacent QWERTY key for char (preserves case), or None."""
    adjacent = _QWERTY_ADJACENT.get(char.lower())
    if not adjacent:
        return None
    wrong = random.choice(adjacent)
    return wrong.upper() if char.isupper() else wrong


class InstagramPage:
    BASE_URL = "https://www.instagram.com"

    def __init__(self, context, timing_multiplier: float = 1.0, extended_browse: bool = False):
        self._context = context
        self._page = None
        self._tm = timing_multiplier  # per-account speed factor (0.80–1.30)
        self._extended_browse = extended_browse  # True for fresh/low-warmup accounts

    async def _get_page(self):
        if not self._page or self._page.is_closed():
            self._page = await self._context.new_page()
        return self._page

    async def ensure_logged_in(self, account_id: str) -> None:
        """Check if we're logged in; if not, perform login."""
        page = await self._get_page()
        await page.goto(self.BASE_URL, wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(1.5, 3.0))

        # Check if we're on the login page
        if "accounts/login" in page.url:
            await self._do_login(account_id, page)
        elif await page.locator('[aria-label="Instagram"]').count() == 0:
            # Something unexpected — try navigating to home
            logger.warning("Unexpected page state, navigating to home")
            await page.goto(self.BASE_URL, wait_until="domcontentloaded")

    async def _do_login(self, account_id: str, page) -> None:
        """Perform Instagram login using stored credentials."""
        from app.database import AsyncSessionLocal
        from app.models.account import InstagramAccount, AccountStatus
        from app.utils.crypto import decrypt
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(InstagramAccount).where(InstagramAccount.id == account_id))
            account = result.scalar_one_or_none()
            if not account:
                raise DMSendError(f"Account {account_id} not found")

            username = account.username
            password = decrypt(account.encrypted_password)

        logger.info(f"Logging in as @{username}...")

        # Fill username
        username_field = page.locator('input[name="username"]')
        await username_field.wait_for(state="visible", timeout=10000)
        await self._human_type(username_field, username)
        await asyncio.sleep(random.uniform(0.3, 0.8))

        # Fill password
        password_field = page.locator('input[name="password"]')
        await self._human_type(password_field, password)
        await asyncio.sleep(random.uniform(0.5, 1.2))

        # Click login
        await page.locator('button[type="submit"]').click()
        await asyncio.sleep(random.uniform(2, 4))

        # Check for challenge
        if "challenge" in page.url or "checkpoint" in page.url:
            raise AccountChallengeError(account_id, page.url)

        # Check for ban / unusual activity
        if await page.locator('text=We suspended your account').count() > 0:
            raise AccountBannedError(f"Account @{username} is suspended")

        logger.info(f"Login successful for @{username}")

    async def _dismiss_ig_modals(self, page, username: str) -> None:
        """Dismiss any Instagram modal/popup overlay (sleep mode, notifications, etc.).
        Safe to call multiple times — only clicks if a matching button is visible.
        Uses has-text (not text-is) to tolerate whitespace differences.
        """
        for selector in [
            'button:has-text("OK")',
            'button:has-text("Not Now")',
            'button:has-text("Not now")',
            'button:has-text("Cancel")',
            'button:has-text("Maybe Later")',
            '[role="button"]:has-text("OK")',
        ]:
            btn = page.locator(selector)
            try:
                if await btn.count() > 0:
                    logger.debug(f"@{username}: dismisso modal ({selector})")
                    await btn.first.click()
                    await asyncio.sleep(random.uniform(0.4, 0.8))
            except Exception:
                pass

    async def _is_stories_viewer_open(self, page) -> bool:
        """Detect Instagram Stories viewer by URL or story-reply UI."""
        if "/stories/" in page.url:
            return True

        selectors = [
            'input[placeholder*="Reply"]',
            'input[placeholder*="Rispondi"]',
            'textarea[placeholder*="Reply"]',
            'textarea[placeholder*="Rispondi"]',
            '[placeholder*="Reply to"]',
            '[placeholder*="Rispondi alla storia"]',
            '[aria-label*="Reply to"]',
            '[aria-label*="Rispondi alla storia"]',
            'div[role="textbox"][aria-label*="Reply"]',
            'div[role="textbox"][aria-label*="Rispondi"]',
        ]
        for selector in selectors:
            try:
                loc = page.locator(selector)
                if await loc.count() > 0 and await loc.first.is_visible():
                    return True
            except Exception:
                pass

        return False

    async def _dismiss_stories_if_open(self, page, username: str) -> None:
        """Close Instagram's Stories viewer before looking for profile DM controls."""
        if not await self._is_stories_viewer_open(page):
            return

        logger.info(f"@{username}: Stories viewer detected - closing before DM")

        for attempt in range(2):
            await page.keyboard.press("Escape")
            await asyncio.sleep(random.uniform(1.2, 2.0))
            if not await self._is_stories_viewer_open(page):
                return

            close_btn = (
                page.locator('svg[aria-label="Close"]')
                .or_(page.locator('svg[aria-label="Chiudi"]'))
                .or_(page.locator('[aria-label="Close"]'))
                .or_(page.locator('[aria-label="Chiudi"]'))
            )
            try:
                if await close_btn.count() > 0:
                    await close_btn.first.click()
                    await asyncio.sleep(random.uniform(1.0, 1.8))
                    if not await self._is_stories_viewer_open(page):
                        return
            except Exception:
                pass

            if attempt == 0:
                logger.info(f"@{username}: Stories still open after close attempt - retrying")

        logger.info(f"@{username}: Stories still open - navigating directly to profile")
        await page.goto(f"{self.BASE_URL}/{username}/", wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(1.5, 2.5))
        return

        """Close Instagram's Stories viewer if it auto-opened on profile navigation.

        The stories viewer has a completely different DOM — no profile header,
        no Message button — so we must escape it before proceeding.
        """
        # URL-based: Instagram redirected to /stories/username/...
        if "/stories/" in page.url:
            logger.info(f"@{username}: redirected to Stories — pressing Escape to return to profile")
            await page.keyboard.press("Escape")
            await asyncio.sleep(random.uniform(1.2, 2.0))
            # If Escape didn't work, navigate directly
            if "/stories/" in page.url:
                logger.info(f"@{username}: Escape failed, navigating directly to profile")
                await page.goto(f"{self.BASE_URL}/{username}/", wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(1.5, 2.5))
            return

        # DOM-based: stories overlay without URL change (stories as modal on profile page).
        # The "Reply to story" input is unique to the stories viewer.
        story_selectors = [
            'textarea[placeholder*="Reply"]',
            'textarea[placeholder*="Rispondi"]',
            '[placeholder*="Reply to"]',
            '[placeholder*="Rispondi alla storia"]',
        ]
        for selector in story_selectors:
            try:
                if await page.locator(selector).count() > 0:
                    logger.info(f"@{username}: Stories overlay detected — pressing Escape")
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(random.uniform(1.2, 2.0))
                    break
            except Exception:
                pass

    async def send_dm(self, username: str, message: str, pre_send_callback: Optional[Callable[[], Awaitable[bool]]] = None) -> None:
        """Navigate to a user's profile and send a DM."""
        page = await self._get_page()

        # Navigate to target's profile
        profile_url = f"{self.BASE_URL}/{username}/"
        logger.debug(f"Navigating to profile: {profile_url}")
        await page.goto(profile_url, wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(1.5, 3.0))

        # Instagram occasionally serves a "Something went wrong" error page.
        # A single reload always fixes it — detect and retry once before giving up.
        if await page.locator('text="Something went wrong"').count() > 0:
            logger.info(f"@{username}: IG error page detected — reloading once")
            await page.reload(wait_until="domcontentloaded")
            await asyncio.sleep(random.uniform(2.0, 3.5))

        # Dismiss modals that may appear on page load
        await self._dismiss_ig_modals(page, username)

        # Close Stories viewer if Instagram auto-opened it instead of profile
        await self._dismiss_stories_if_open(page, username)

        # Check if profile exists / account is private / page not available
        if await page.locator('text=Sorry, this page isn').count() > 0:
            raise DMRestrictedError(f"Profile @{username} not found or unavailable")

        # Browse profile briefly (human-like) — duration scaled by per-account timing multiplier.
        # Private profiles get a short browse: a human sees the lock icon and acts immediately.
        # Fresh / low-warmup accounts get extended browse (90-360s) to dilute "login → DM" pattern.
        # Detect private account via JS regex on body text — more reliable than
        # DOM selectors which break when IG changes element structure or text.
        # Pattern covers EN/IT/FR/DE and any capitalization variant.
        is_private: bool = await page.evaluate(
            "() => /this account is private|questo account.{0,10}privat|ce compte est priv|dieses konto ist privat/i"
            ".test(document.body.innerText)"
        )
        # Sparse = public but fewer than 3 visible posts (new/inactive accounts).
        # A human glances at the empty grid and moves straight to DM — no long scrolling.
        is_sparse: bool = (not is_private) and await page.evaluate("""
            () => {
                const posts = document.querySelectorAll(
                    'main article a[href*="/p/"], main article a[href*="/reel/"]'
                );
                return posts.length < 3;
            }
        """)
        if is_private:
            browse_time = random.uniform(1.5, 4.0) * self._tm
            logger.info(f"@{username}: private profile — short browse {browse_time:.0f}s")
        elif is_sparse:
            browse_time = random.uniform(3.0, 8.0) * self._tm
            logger.info(f"@{username}: sparse profile (<3 posts) — short browse {browse_time:.0f}s")
        elif self._extended_browse:
            browse_time = extended_pre_dm_browse_seconds() * self._tm
            logger.info(f"Extended browse for fresh account: @{username} {browse_time:.0f}s")
        else:
            browse_time = pre_dm_browse_seconds() * self._tm
            logger.debug(f"Browsing @{username}'s profile for {browse_time:.0f}s (tm={self._tm:.2f})")

        # Attempt to view stories before scrolling profile (~35% of visits, non-private/sparse only).
        # If stories open, watches briefly then closes them before continuing.
        if not is_private and not is_sparse:
            await self._maybe_view_stories(page, username)

        await self._simulate_browsing(page, browse_time)

        # Scroll back to top so the Message button is visible again
        await page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
        await asyncio.sleep(random.uniform(0.8, 1.5))

        # Dismiss again — sleep mode popup can appear mid-browsing
        await self._dismiss_ig_modals(page, username)

        # Safety net: if stories are still open (e.g. _maybe_view_stories close failed),
        # dismiss them now before attempting to find the Message button.
        await self._dismiss_stories_if_open(page, username)

        # Click "Message" button (handles EN/IT locale and mutual-follow layout)
        await self._click_message_button(page, username)

        # Wait for navigation to DM thread (/direct/t/...)
        navigated_to_direct = False
        try:
            await page.wait_for_url(lambda url: '/direct/' in url, timeout=8000)
            navigated_to_direct = True
            logger.debug(f"@{username}: navigato al thread DM ({page.url})")
        except Exception:
            logger.debug(f"@{username}: nessuna navigazione a /direct/ — controllo modal (URL: {page.url})")

        # Wait for the UI to fully settle after navigation/modal open
        await asyncio.sleep(random.uniform(2.5, 4.0))

        # Dismiss any popup that appeared after DM thread opened
        await self._dismiss_ig_modals(page, username)

        # Find DM input — ordered from most specific to least.
        # NOTE: placeholder attr doesn't work on contenteditable divs — removed.
        # role="textbox" is the most reliable cross-locale selector on /direct/ pages.
        msg_input = None
        found_selector = None
        for selector in [
            'div[aria-label="Message"][contenteditable="true"]',
            'div[aria-label="Messaggio"][contenteditable="true"]',
            'div[aria-label="Message..."][contenteditable="true"]',
            'div[contenteditable="true"][role="textbox"]',
            'div[role="textbox"]',
        ]:
            loc = page.locator(selector)
            try:
                await loc.first.wait_for(state="visible", timeout=8000)
                msg_input = loc.first
                found_selector = selector
                break
            except Exception:
                continue

        if msg_input is None and await self._is_stories_viewer_open(page):
            logger.info(f"@{username}: DM input search landed in Stories - returning to profile and retrying once")
            await self._dismiss_stories_if_open(page, username)
            await self._click_message_button(page, username)
            try:
                await page.wait_for_url(lambda url: '/direct/' in url, timeout=8000)
                navigated_to_direct = True
                logger.debug(f"@{username}: navigato al thread DM ({page.url})")
            except Exception:
                logger.debug(f"@{username}: retry DM click did not navigate - checking modal (URL: {page.url})")
            await asyncio.sleep(random.uniform(2.5, 4.0))
            await self._dismiss_ig_modals(page, username)

            for selector in [
                'div[aria-label="Message"][contenteditable="true"]',
                'div[aria-label="Messaggio"][contenteditable="true"]',
                'div[aria-label="Message..."][contenteditable="true"]',
                'div[contenteditable="true"][role="textbox"]',
                'div[role="textbox"]',
            ]:
                loc = page.locator(selector)
                try:
                    await loc.first.wait_for(state="visible", timeout=8000)
                    msg_input = loc.first
                    found_selector = selector
                    break
                except Exception:
                    continue

        if msg_input is None:
            try:
                path = f"data/debug_no_input_{username}.png"
                await page.screenshot(path=path)
                from app.services.notifier import send_telegram_photo
                asyncio.create_task(send_telegram_photo(
                    path,
                    caption=f"Critical DM error: input non trovato per @{username}",
                    level="error",
                ))
                logger.warning(f"@{username}: screenshot → data/debug_no_input_{username}.png")
            except Exception:
                pass
            if not navigated_to_direct:
                raise DMSendError(
                    f"@{username}: click 'Message' non ha aperto thread DM "
                    f"(URL: {page.url}). Possibile miss del click o layout cambiato."
                )
            raise DMSendError(f"@{username}: DM input non trovato dopo navigazione a /direct/")

        logger.debug(f"@{username}: input trovato — selettore: {found_selector}")

        # Small extra pause so the input is fully interactive before typing
        await asyncio.sleep(random.uniform(0.5, 1.0))

        # Strip any newlines — keyboard.type('\n') triggers Instagram's send action
        # mid-typing, splitting one message into two (or losing the second half).
        # This catches both newly generated messages and old DB records with \n.
        import re as _re
        message = _re.sub(r'\s*[\r\n]+\s*', ' ', message).strip()

        # Type and send the message
        await self._human_type(msg_input, message)
        await asyncio.sleep(random.uniform(0.5, 1.5))

        # Pre-send safety check: abort if another worker already processed this follower.
        # This must run just before pressing Enter so the DM is never delivered
        # if the check fails. DMAbortedBeforeSendError is raised BEFORE Enter,
        # so message.status='sending' was set but the DM was NOT delivered.
        if pre_send_callback is not None:
            should_proceed = await pre_send_callback()
            if not should_proceed:
                raise DMAbortedBeforeSendError(
                    f"@{username}: pre-send check returned False - aborting before Enter"
                )

        # Send by pressing Enter
        await page.keyboard.press("Enter")
        await asyncio.sleep(random.uniform(1, 2))

        logger.info(f"DM sent to @{username}")

        # Post-DM dwell: linger in thread reading own message + scroll up to read past msgs.
        # Eliminates the "send → instant close" pattern that looks scripted.
        dwell = post_dm_dwell_seconds() * self._tm
        try:
            await self._post_dm_linger(page, dwell)
        except Exception as e:
            logger.debug(f"@{username}: post-DM dwell skipped ({e})")

    async def _human_type(self, element, text: str) -> None:
        """Type text with human-like variable speed and word-level pauses.

        Clicks the element to focus it, then uses page.keyboard for all subsequent
        key events. This avoids re-locating the element on every character, which
        can fail if Instagram's React DOM re-renders during typing.
        """
        await element.click()
        await asyncio.sleep(random.uniform(0.2, 0.5))

        # After the click, the element has focus. Using page.keyboard sends events
        # directly to the focused element without evaluating the locator again.
        page = self._page

        # Each session has a random base typing speed, scaled by per-account timing multiplier
        base_ms = random.uniform(70, 160) * self._tm

        words = text.split(' ')
        for i, word in enumerate(words):
            # Occasional thinking pause before a word (more likely mid-sentence)
            if i > 0 and random.random() < 0.15:
                await asyncio.sleep(random.uniform(0.4, 1.8))

            for char_idx, char in enumerate(word):
                # Typo: ~8% chance per char in words >3 letters (skip first/last char)
                if len(word) > 3 and 0 < char_idx < len(word) - 1 and random.random() < 0.08:
                    wrong = _typo_char(char)
                    if wrong:
                        err_delay = random.lognormvariate(math.log(base_ms), 0.45)
                        await page.keyboard.type(wrong)
                        await asyncio.sleep(max(35, min(480, err_delay)) / 1000)
                        await asyncio.sleep(random.uniform(0.15, 0.50))  # notice mistake
                        await page.keyboard.press("Backspace")
                        await asyncio.sleep(random.uniform(0.08, 0.25))  # before retyping

                # Correct character with lognormal delay
                delay_ms = random.lognormvariate(math.log(base_ms), 0.45)
                delay_ms = max(35, min(480, delay_ms))
                await page.keyboard.type(char)
                await asyncio.sleep(delay_ms / 1000)
                # Rare micro-pause within a word (re-reading, hesitation)
                if random.random() < 0.03:
                    await asyncio.sleep(random.uniform(0.2, 0.7))

            # Type the space between words
            if i < len(words) - 1:
                await page.keyboard.type(' ')
                await asyncio.sleep(random.uniform(40, 120) / 1000)

    async def _maybe_view_stories(self, page, username: str) -> bool:
        """Click the profile picture to open stories if available (≈35% of visits).
        Watches 6-22s, occasionally taps to advance, then closes with Escape.
        Falls back silently on any error. Returns True if stories were viewed.
        """
        if random.random() > 0.35:
            return False
        try:
            story_btn = page.locator("main header img").first
            if await story_btn.count() == 0:
                return False

            await self._human_click(page, story_btn)
            await asyncio.sleep(random.uniform(1.2, 2.5))

            # Detect whether stories actually opened
            story_open = "/stories/" in page.url
            if not story_open:
                reply_input = page.locator(
                    'textarea[placeholder*="Reply"], textarea[placeholder*="Rispondi"]'
                )
                story_open = await reply_input.count() > 0

            if not story_open:
                logger.debug(f"@{username}: clicked profile picture — no stories available")
                return False

            watch_time = random.uniform(6.0, 22.0) * self._tm
            logger.info(f"@{username}: viewing stories for {watch_time:.0f}s")
            end_time = asyncio.get_event_loop().time() + watch_time
            while asyncio.get_event_loop().time() < end_time:
                await asyncio.sleep(random.uniform(2.0, 5.0))
                # 30% chance: tap right side to advance to next story
                if random.random() < 0.30:
                    try:
                        await page.mouse.click(
                            random.randint(560, 680), random.randint(200, 400)
                        )
                        await asyncio.sleep(random.uniform(0.5, 1.5))
                    except Exception:
                        pass

            # Close stories and return to profile
            await page.keyboard.press("Escape")
            await asyncio.sleep(random.uniform(1.0, 2.0))

            # Verify stories actually closed — check both URL redirect AND overlay DOM.
            # Overlay-style stories don't change the URL, so URL check alone is not enough.
            still_open = "/stories/" in page.url
            if not still_open:
                try:
                    reply_check = page.locator(
                        'textarea[placeholder*="Reply"], textarea[placeholder*="Rispondi"]'
                    )
                    still_open = await reply_check.count() > 0
                except Exception:
                    pass

            if still_open:
                logger.info(f"@{username}: Escape didn't close stories — navigating directly to profile")
                await page.goto(f"{self.BASE_URL}/{username}/", wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(1.5, 2.5))

            logger.debug(f"@{username}: stories closed, back on profile")
            return True

        except Exception as e:
            logger.debug(f"@{username}: _maybe_view_stories error ({type(e).__name__}) — skipping")
            try:
                if "/stories/" in page.url:
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(1.0)
                    if "/stories/" in page.url:
                        await page.goto(f"{self.BASE_URL}/{username}/", wait_until="domcontentloaded")
                        await asyncio.sleep(1.5)
            except Exception:
                pass
            return False

    async def _click_message_button(self, page, username: str) -> None:
        """
        Find and click the Message button on a profile page.

        Two strategies (fast fallback via .or_() combinators):
        1. Direct "Message"/"Messaggio" button in profile header
        2. Three dots menu (⋯) → "Send message"/"Invia messaggio"
        """
        # Wait for profile header to render
        await asyncio.sleep(random.uniform(1.0, 2.0))

        # ── Strategy 1: Direct "Message" / "Messaggio" button ──
        # IMPORTANT: scope to `main header` (profile header inside main content),
        # NOT bare `header` — IG's left sidebar/topbar can also be a <header>
        # and contain "Notifications"/"Messages" nav items.
        direct_msg = (
            page.locator('main header div[role="button"]:text-is("Message")')
            .or_(page.locator('main header a[role="button"]:text-is("Message")'))
            .or_(page.locator('main header button:text-is("Message")'))
            .or_(page.locator('main header div[role="button"]:text-is("Messaggio")'))
            .or_(page.locator('main header a[role="button"]:text-is("Messaggio")'))
            .or_(page.locator('main header button:text-is("Messaggio")'))
            # Fallback if Instagram drops <header> in profile redesign
            .or_(page.locator('main section div[role="button"]:text-is("Message")'))
            .or_(page.locator('main section div[role="button"]:text-is("Messaggio")'))
        )

        try:
            await direct_msg.first.wait_for(state="visible", timeout=3000)
            # Snapshot URL pre-click — verify navigation post-click
            url_before = page.url
            await self._click_and_verify_navigation(
                page, direct_msg.first, username, url_before, label="direct Message"
            )
            return
        except DMSendError:
            raise
        except Exception:
            logger.debug(f"@{username}: no direct Message button visible")

        # ── Strategy 2: Three dots (⋯) → "Send message" ──
        logger.info(f"@{username}: falling back to three dots menu")

        # 2a. Find and click the three dots button
        three_dots = (
            page.locator('[aria-label="Options"]')
            .or_(page.locator('[aria-label="More options"]'))
            .or_(page.locator('[aria-label="Opzioni"]'))
            .or_(page.locator('[aria-label="Altre opzioni"]'))
            .or_(page.locator('[aria-label="Altro"]'))
            .or_(page.locator('[aria-label="More"]'))
            .or_(page.locator('div[role="button"]:has(svg[aria-label="Options"])'))
            .or_(page.locator('div[role="button"]:has(svg[aria-label="Altre opzioni"])'))
            .or_(page.locator('button:has(svg[aria-label="Options"])'))
            .or_(page.locator('button:has(svg[aria-label="Altre opzioni"])'))
        )

        try:
            await three_dots.first.wait_for(state="visible", timeout=4000)
        except Exception:
            # Debug screenshot before failing
            try:
                path = f"data/debug_no_menu_{username}.png"
                await page.screenshot(path=path)
                from app.services.notifier import send_telegram_photo
                asyncio.create_task(send_telegram_photo(
                    path,
                    caption=f"Critical DM error: menu profilo non trovato per @{username}",
                    level="error",
                ))
                logger.warning(f"@{username}: debug screenshot → data/debug_no_menu_{username}.png")
            except Exception:
                pass
            raise DMRestrictedError(
                f"@{username}: neither Message button nor three dots menu found on profile"
            )

        await self._human_click(page, three_dots.first)
        await asyncio.sleep(random.uniform(1.0, 2.5))

        # Wait for the menu modal to finish loading (spinner disappears).
        # Instagram renders the menu items asynchronously — clicking too early
        # produces a blank modal with a spinner and no "Send message" item.
        try:
            await page.wait_for_function(
                "() => document.querySelector('[role=\"dialog\"] [role=\"progressbar\"], "
                "[role=\"dialog\"] svg[aria-label=\"Loading\"], "
                "[role=\"dialog\"] div[class*=\"spinner\"]') === null",
                timeout=6000,
            )
        except Exception:
            pass  # no spinner found or already gone — proceed anyway
        await asyncio.sleep(random.uniform(0.3, 0.7))

        # 2b. Find "Send message" / "Invia messaggio" in the opened menu/dialog
        send_msg = (
            page.locator('button:text-is("Send message")')
            .or_(page.locator('button:text-is("Send Message")'))
            .or_(page.locator('button:text-is("Invia messaggio")'))
            .or_(page.locator('button:text-is("Invia un messaggio")'))
            .or_(page.locator('button:has-text("Send message")'))
            .or_(page.locator('button:has-text("Send Message")'))
            .or_(page.locator('button:has-text("Invia messaggio")'))
            .or_(page.locator('button:has-text("Invia un messaggio")'))
            .or_(page.locator('[role="menuitem"]:has-text("Send message")'))
            .or_(page.locator('[role="menuitem"]:has-text("Invia messaggio")'))
            .or_(page.locator('div[role="button"]:has-text("Send message")'))
            .or_(page.locator('div[role="button"]:has-text("Invia messaggio")'))
            .or_(page.locator('div[role="button"]:has-text("Invia un messaggio")'))
            .or_(page.locator('a:has-text("Send message")'))
            .or_(page.locator('a:has-text("Invia messaggio")'))
        )

        try:
            await send_msg.first.wait_for(state="visible", timeout=12000)
        except Exception:
            # Debug screenshot before failing
            try:
                path = f"data/debug_no_send_msg_{username}.png"
                await page.screenshot(path=path)
                from app.services.notifier import send_telegram_photo
                asyncio.create_task(send_telegram_photo(
                    path,
                    caption=f"Critical DM error: voce Send message non trovata per @{username}",
                    level="error",
                ))
                logger.warning(f"@{username}: debug screenshot → data/debug_no_send_msg_{username}.png")
            except Exception:
                pass
            raise DMRestrictedError(f"@{username}: 'Send message' not found in three dots menu")

        # Menu "Send message" may open a MODAL instead of navigating —
        # outer send_dm() handles both cases, so skip URL verification here.
        await self._human_click(page, send_msg.first)
        logger.info(f"@{username}: clicked 'Send message' from three dots menu")

    async def _click_and_verify_navigation(
        self, page, element, username: str, url_before: str, label: str
    ) -> None:
        """
        Click an element with human-like motion, then verify navigation.
        If the click misses (URL unchanged or wrong destination), retry once
        with Playwright's element.click() which auto-scrolls and re-resolves
        bounding box. Raises DMSendError if both attempts fail.
        """
        try:
            await element.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        await asyncio.sleep(random.uniform(0.2, 0.5))

        # Attempt 1: human-like mouse motion + click on fresh bbox
        await self._human_click(page, element)

        # Verify click succeeded within a reasonable window. Two success modes:
        #   A) URL navigated to /direct/ (classic redirect to DM thread)
        #   B) DM textbox became visible in-page (modal/overlay variant — IG
        #      sometimes opens DM thread as overlay without changing URL)
        # Either is a valid "Message button worked" signal. Misroute (URL
        # changed to /notifications etc.) handled below.
        try:
            await page.wait_for_function(
                """(prev) => {
                    if (location.href !== prev && location.href.includes('/direct/')) return true;
                    const tb = document.querySelector(
                        'div[aria-label="Message"][contenteditable="true"], '
                        + 'div[aria-label="Messaggio"][contenteditable="true"], '
                        + 'div[contenteditable="true"][role="textbox"]'
                    );
                    if (!tb) return false;
                    const r = tb.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                }""",
                arg=url_before,
                timeout=4000,
            )
            logger.info(f"@{username}: clicked {label} → {page.url}")
            return
        except Exception:
            pass

        # Misroute detection — if landed on notifications or stayed on profile
        cur = page.url
        if "/notifications" in cur:
            logger.warning(
                f"@{username}: click {label} hit Notifications instead of Messages "
                f"(URL: {cur}) — going back to retry"
            )
            try:
                await page.go_back(wait_until="domcontentloaded", timeout=5000)
                await asyncio.sleep(random.uniform(1.5, 2.5))
            except Exception:
                pass

        # Attempt 2: Playwright's built-in click — auto scroll + re-resolve bbox
        logger.info(f"@{username}: retrying {label} click via element.click()")
        try:
            await element.click(timeout=3000)
        except Exception as e:
            raise DMSendError(
                f"@{username}: retry click {label} failed: {type(e).__name__}: {str(e)[:120]}"
            )

        try:
            await page.wait_for_function(
                """(prev) => {
                    if (location.href !== prev && location.href.includes('/direct/')) return true;
                    const tb = document.querySelector(
                        'div[aria-label="Message"][contenteditable="true"], '
                        + 'div[aria-label="Messaggio"][contenteditable="true"], '
                        + 'div[contenteditable="true"][role="textbox"]'
                    );
                    if (!tb) return false;
                    const r = tb.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                }""",
                arg=url_before,
                timeout=5000,
            )
            logger.info(f"@{username}: retry {label} succeeded → {page.url}")
        except Exception:
            raise DMSendError(
                f"@{username}: click {label} non ha aperto thread DM né modal "
                f"(URL finale: {page.url}). Layout possibly changed o click missed."
            )

    async def _human_click(self, page, element) -> None:
        """Click an element at a random position within its bounding box.
        bbox computed immediately before click to minimize stale-coords risk
        from layout shift. Falls back to element.click() if bbox unavailable.
        """
        try:
            await element.scroll_into_view_if_needed(timeout=1500)
        except Exception:
            pass
        box = await element.bounding_box()
        if box:
            x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
            y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
            await page.mouse.move(x, y, steps=random.randint(5, 15))
            # Short pause only — long sleep here lets layout shift invalidate coords
            await asyncio.sleep(random.uniform(0.05, 0.15))
            await page.mouse.click(x, y)
        else:
            await element.click()

    async def _simulate_browsing(self, page, duration_seconds: float) -> None:
        """Simulate a human browsing a profile: varied scrolling, reading pauses, backtracking."""
        end_time = asyncio.get_event_loop().time() + duration_seconds
        prev_scroll_y: float = await page.evaluate("window.scrollY")
        scroll_stale = 0  # consecutive scroll-down attempts with no position change

        while asyncio.get_event_loop().time() < end_time:
            action = random.random()

            # When at page bottom (3+ stale scroll-downs), skip scroll actions entirely —
            # a human sees there's nothing left and just reads / moves the mouse.
            at_bottom = scroll_stale >= 3

            if action < 0.50 and not at_bottom:
                # Normal scroll down — variable amount (small touchpad-like vs big swipe)
                if random.random() < 0.4:
                    # Small incremental scroll (touchpad)
                    steps = random.randint(2, 5)
                    for _ in range(steps):
                        await page.mouse.wheel(0, random.randint(60, 180))
                        await asyncio.sleep(random.uniform(0.05, 0.15))
                else:
                    # Single bigger scroll
                    await page.mouse.wheel(0, random.randint(150, 700))
                await asyncio.sleep(random.uniform(0.6, 2.2))
                new_y: float = await page.evaluate("window.scrollY")
                if new_y <= prev_scroll_y:
                    scroll_stale += 1
                else:
                    scroll_stale = 0
                prev_scroll_y = new_y

            elif action < (0.85 if at_bottom else 0.70):
                # Reading pause — stopped, looking at content
                await asyncio.sleep(random.uniform(1.5, 4.0))

            elif action < 0.85 and not at_bottom:
                # Scroll back up a bit (re-reading something)
                await page.mouse.wheel(0, -random.randint(80, 350))
                await asyncio.sleep(random.uniform(0.8, 2.0))
                prev_scroll_y = await page.evaluate("window.scrollY")
                scroll_stale = 0  # scrolled up — bottom detection resets

            else:
                # Move mouse around (hovering over a photo or link)
                x = random.randint(200, 900)
                y = random.randint(200, 700)
                await page.mouse.move(x, y, steps=random.randint(3, 8))
                await asyncio.sleep(random.uniform(0.5, 1.5))

    async def _post_dm_linger(self, page, duration_seconds: float) -> None:
        """Stay in DM thread after sending: scroll up occasionally, mouse idle, reading pauses."""
        end_time = asyncio.get_event_loop().time() + duration_seconds
        while asyncio.get_event_loop().time() < end_time:
            r = random.random()
            if r < 0.35:
                # Scroll up to re-read past messages
                await page.mouse.wheel(0, -random.randint(60, 220))
                await asyncio.sleep(random.uniform(0.8, 2.0))
            elif r < 0.55:
                # Reading pause
                await asyncio.sleep(random.uniform(1.2, 3.5))
            elif r < 0.75:
                # Mouse idle move
                x = random.randint(300, 1000)
                y = random.randint(300, 700)
                await page.mouse.move(x, y, steps=random.randint(3, 8))
                await asyncio.sleep(random.uniform(0.6, 1.6))
            else:
                # Small scroll down (return toward latest msg)
                await page.mouse.wheel(0, random.randint(40, 150))
                await asyncio.sleep(random.uniform(0.8, 1.8))

    async def browse_feed(self, duration_seconds: float) -> None:
        """
        Ambient activity on the home feed: scroll, occasional like (~3%),
        open 0-2 posts and view them 1-30s, mouse idle. Used to dilute the
        'login → DM → close' bot pattern.

        Defensive: any failure in like / open-post is swallowed and logged at
        debug level — never raises (must not break the surrounding DM flow).
        """
        try:
            page = await self._get_page()
        except Exception as e:
            logger.warning(f"browse_feed: cannot get page ({e}), falling back to sleep")
            await asyncio.sleep(duration_seconds)
            return

        # Navigate to home feed if not already there
        try:
            cur = page.url or ""
            if "/direct/" in cur or not cur.startswith(self.BASE_URL) or cur.rstrip("/") != self.BASE_URL:
                # Try clicking IG logo / Home nav first (more human than typing URL)
                clicked = False
                try:
                    home_nav = page.locator(
                        'a[href="/"][role="link"], svg[aria-label="Home"], svg[aria-label="Instagram"]'
                    ).first
                    if await home_nav.count() > 0:
                        await home_nav.click(timeout=2500)
                        await asyncio.sleep(random.uniform(1.5, 3.0))
                        clicked = True
                except Exception:
                    pass
                if not clicked:
                    await page.goto(self.BASE_URL + "/", wait_until="domcontentloaded")
                    await asyncio.sleep(random.uniform(1.8, 3.2))
        except Exception as e:
            logger.debug(f"browse_feed: navigation issue ({e}) — continuing on current page")

        # Dismiss any popup that may appear on home (sleep mode, notifications opt-in)
        try:
            await self._dismiss_ig_modals(page, "feed")
        except Exception:
            pass

        end_time = asyncio.get_event_loop().time() + duration_seconds
        posts_opened = 0
        max_posts = random.choice([0, 1, 1, 2])  # weighted: usually 0-1
        like_done = False
        like_target_prob = random.random() < 0.35  # ~35% sessions actually like a post
        likes_in_session = 0
        max_likes = random.choice([0, 0, 1, 1, 2]) if like_target_prob else 0

        logger.info(
            f"[Ambient] Feed browse {duration_seconds:.0f}s "
            f"(max_posts={max_posts}, max_likes={max_likes})"
        )

        while asyncio.get_event_loop().time() < end_time:
            remaining = end_time - asyncio.get_event_loop().time()
            r = random.random()

            if r < 0.55:
                # Scroll feed
                if random.random() < 0.45:
                    steps = random.randint(2, 5)
                    for _ in range(steps):
                        await page.mouse.wheel(0, random.randint(80, 220))
                        await asyncio.sleep(random.uniform(0.05, 0.18))
                else:
                    await page.mouse.wheel(0, random.randint(200, 800))
                await asyncio.sleep(random.uniform(1.2, 4.0))

            elif r < 0.70:
                # Reading pause
                await asyncio.sleep(random.uniform(2.0, 6.0))

            elif r < 0.78 and likes_in_session < max_likes and remaining > 4:
                # Like a visible post (rare)
                try:
                    like_btn = page.locator(
                        'article svg[aria-label="Like"], article svg[aria-label="Mi piace"]'
                    ).first
                    if await like_btn.count() > 0:
                        box = await like_btn.bounding_box()
                        if box and 100 < box["y"] < 700:
                            cx = box["x"] + box["width"] / 2
                            cy = box["y"] + box["height"] / 2
                            await page.mouse.move(cx, cy, steps=random.randint(5, 12))
                            await asyncio.sleep(random.uniform(0.3, 0.8))
                            await page.mouse.click(cx, cy)
                            likes_in_session += 1
                            logger.info(f"[Ambient] Liked a feed post ({likes_in_session}/{max_likes})")
                            await asyncio.sleep(random.uniform(0.8, 2.0))
                except Exception as e:
                    logger.debug(f"[Ambient] like skipped ({type(e).__name__})")

            elif r < 0.88 and posts_opened < max_posts and remaining > 12:
                # Open a post and view 1-30s
                try:
                    post_link = page.locator(
                        'article a[href*="/p/"], article a[href*="/reel/"]'
                    ).first
                    if await post_link.count() > 0:
                        box = await post_link.bounding_box()
                        if box and 100 < box["y"] < 700:
                            await page.mouse.move(
                                box["x"] + 20, box["y"] + 20, steps=random.randint(4, 10)
                            )
                            await asyncio.sleep(random.uniform(0.2, 0.6))
                            await post_link.click(timeout=3000)
                            view_time = random.uniform(1.0, min(30.0, max(2.0, remaining - 3)))
                            posts_opened += 1
                            logger.info(
                                f"[Ambient] Opened post ({posts_opened}/{max_posts}), viewing {view_time:.0f}s"
                            )
                            elapsed = 0.0
                            post_scroll_y: float = await page.evaluate("window.scrollY")
                            post_scroll_stale = 0
                            while elapsed < view_time:
                                chunk = min(random.uniform(1.0, 3.5), view_time - elapsed)
                                await asyncio.sleep(chunk)
                                elapsed += chunk
                                if random.random() < 0.30 and post_scroll_stale < 3:
                                    await page.mouse.wheel(0, random.randint(60, 200))
                                    new_post_y: float = await page.evaluate("window.scrollY")
                                    if new_post_y <= post_scroll_y:
                                        post_scroll_stale += 1
                                    else:
                                        post_scroll_stale = 0
                                    post_scroll_y = new_post_y
                            try:
                                await page.keyboard.press("Escape")
                            except Exception:
                                pass
                            await asyncio.sleep(random.uniform(0.6, 1.6))
                except Exception as e:
                    logger.debug(f"[Ambient] open post skipped ({type(e).__name__})")

            else:
                # Mouse idle
                x = random.randint(250, 1050)
                y = random.randint(150, 700)
                await page.mouse.move(x, y, steps=random.randint(3, 8))
                await asyncio.sleep(random.uniform(0.5, 1.6))

        logger.info(
            f"[Ambient] Feed browse done — opened {posts_opened} posts, "
            f"liked {likes_in_session} ({duration_seconds:.0f}s)"
        )
