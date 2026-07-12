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
    DMAbortedBeforeSendError, AccountSessionExpiredError,
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

    async def ensure_logged_in(self, account_id: str, allow_login: bool = True) -> None:
        """Check if we're logged in; if not, perform login.

        allow_login=False (usato dallo SCRAPING): se la sessione e' scaduta NON
        fa login automatico — solleva AccountSessionExpiredError. Il login
        automatico via credenziali e' un rischio-ban (redirect a challenge il
        giorno dopo) e va fatto solo a mano dall'operatore ('Login Browser').
        """
        page = await self._get_page()
        await page.goto(self.BASE_URL, wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(1.5, 3.0))

        # Check if we're on the login page
        if "accounts/login" in page.url:
            if not allow_login:
                raise AccountSessionExpiredError(account_id)
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

        Selettori ESATTI (:text-is) e scoped a div[role="dialog"] — mai broad.
        Il vecchio `[role="button"]:has-text("OK")` era substring case-insensitive:
        matchava il cerchio highlight "New Bo(OK)"/"Tshirts bo(OK)s" sul profilo e
        il bot apriva il viewer storie al posto di chiudere un popup (causa reale
        dei fallimenti 'finisce nelle storie', 2 casi confermati da screenshot).
        Un "OK"/"Non ora" legittimo sta dentro un dialog, non sul profilo.
        """
        for selector in [
            'div[role="dialog"] button:text-is("OK")',
            'div[role="dialog"] button:text-is("Ok")',
            'div[role="dialog"] button:text-is("Not Now")',
            'div[role="dialog"] button:text-is("Not now")',
            'div[role="dialog"] button:text-is("Non ora")',
            'div[role="dialog"] button:text-is("Cancel")',
            'div[role="dialog"] button:text-is("Annulla")',
            'div[role="dialog"] button:text-is("Maybe Later")',
            'div[role="dialog"] button:text-is("Più tardi")',
            'div[role="dialog"] [role="button"]:text-is("OK")',
            'div[role="dialog"] [role="button"]:text-is("Not Now")',
            'div[role="dialog"] [role="button"]:text-is("Non ora")',
        ]:
            btn = page.locator(selector)
            try:
                if await btn.count() > 0:
                    logger.debug(f"@{username}: dismisso modal ({selector})")
                    await btn.first.click()
                    await asyncio.sleep(random.uniform(0.4, 0.8))
            except Exception:
                pass

    async def _dismiss_blocking_dialog(self, page, username: str) -> None:
        """Chiude un dialog che copre il profilo PRIMA di cercare il bottone
        Messaggio. Caso reale: il modale 'Link' (link-in-bio con piu' link), aperto
        da un tap finito sul profilo — sta sopra tutto e fa fallire il click su
        Messaggio. Preme Escape; se resta, clicca la X del dialog. No-op se siamo
        gia' nel thread DM (li' un dialog e' legittimo e Escape lo chiuderebbe)."""
        try:
            if "/direct/" in page.url:
                return
            dialog = page.locator('div[role="dialog"]')
            if await dialog.count() == 0:
                return
            logger.info(
                f"@{username}: dialog aperto sul profilo (probabile modale 'Link') "
                f"— chiudo prima del click su Messaggio"
            )
            await page.keyboard.press("Escape")
            await asyncio.sleep(random.uniform(0.6, 1.2))
            if await page.locator('div[role="dialog"]').count() > 0:
                for sel in (
                    'div[role="dialog"] [aria-label="Close"]',
                    'div[role="dialog"] [aria-label="Chiudi"]',
                    'div[role="dialog"] svg[aria-label="Close"]',
                    'div[role="dialog"] svg[aria-label="Chiudi"]',
                ):
                    try:
                        x = page.locator(sel)
                        if await x.count() > 0:
                            await x.first.click()
                            await asyncio.sleep(random.uniform(0.5, 1.0))
                            break
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"@{username}: _dismiss_blocking_dialog error ({e})")

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

    # Candidati input DM, dal più specifico al più generico (cross-locale).
    _DM_INPUT_SELECTORS = (
        'div[aria-label="Message"][contenteditable="true"]',
        'div[aria-label="Messaggio"][contenteditable="true"]',
        'div[aria-label="Message..."][contenteditable="true"]',
        'div[contenteditable="true"][role="textbox"]',
        'div[role="textbox"]',
    )

    async def _locate_dm_input(self, page, timeout: int = 10000):
        """Trova l'input DM con UNA sola attesa sull'unione dei selettori, poi
        sceglie per priorità con check istantanei (is_visible).

        Il vecchio loop faceva `wait_for(timeout=8000)` per OGNI selettore in
        sequenza: se i primi (es. aria-label inglese "Message") non matchavano
        il locale IT, si accumulavano 8-24s di attesa a vuoto ("imbambolato").
        Qui il timeout è pagato una volta sola.
        Ritorna (locator | None, selettore | None).
        """
        union = ", ".join(self._DM_INPUT_SELECTORS)
        try:
            await page.locator(union).first.wait_for(state="visible", timeout=timeout)
        except Exception:
            return None, None
        for selector in self._DM_INPUT_SELECTORS:
            loc = page.locator(selector).first
            try:
                if await loc.count() > 0 and await loc.is_visible():
                    return loc, selector
            except Exception:
                continue
        # Union visibile ma priorità mancata (race DOM): usa il primo dell'unione.
        return page.locator(union).first, union

    async def _verify_dm_thread(self, page, username: str) -> bool:
        """True se il thread DM aperto appartiene DAVVERO a `username`.

        Guardia anti-misinstradamento. Il click su "Messaggio" a volte atterra
        sull'inbox DM invece che sul thread del bersaglio; l'inbox tiene aperta
        la conversazione in cima (spesso di un'ALTRA persona). Senza questo
        controllo il bot scriveva nel thread sbagliato — caso reale: 26 DM di
        lead diversi finiti tutti nella chat di @giovanni1927, con i lead
        segnati 'sent' ma mai realmente contattati.

        Segnale: nell'header della conversazione aperta c'e' il link al profilo
        del destinatario (`a[href="/username/"]`). E' specifico del thread
        aperto: la lista-thread laterale linka `/direct/t/<id>`, non `/username/`.
        Un lead a freddo non e' nella sidebar, quindi se il suo link non c'e'
        vuol dire che il suo thread NON e' aperto -> abort prima di scrivere.
        """
        u = username.lower().lstrip("@").strip()
        if not u:
            return False
        for sel in (f'a[href="/{u}/"]', f'a[href="/{u}"]'):
            try:
                loc = page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def send_dm(self, username: str, message: str, pre_send_callback: Optional[Callable[[], Awaitable[bool]]] = None, on_enter: Optional[Callable[[], Awaitable[None]]] = None) -> None:
        """Navigate to a user's profile and send a DM.

        on_enter: callback awaitato SUBITO dopo aver premuto Invio (il punto di non
        ritorno: il DM e' partito). Il chiamante lo usa per marcare 'sending' SOLO
        da qui in poi — cosi' un fallimento PRIMA dell'Invio non lascia mai il
        messaggio in 'sending' (niente reconciliation via API).
        """
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

        # Check if profile exists / account is private / page not available.
        # IG serve la pagina "non disponibile" nella lingua della UI: il match
        # deve essere multilingua (il vecchio check solo-EN lasciava proseguire
        # il bot alla cieca con UI italiana — caso reale rubina.cartomanzia).
        page_gone: bool = await page.evaluate(
            "() => /sorry, this page isn|page isn't available|"
            "spiacenti, questa pagina non|pagina non .{0,3}disponibile|"
            "cette page n'est pas disponible|seite ist leider nicht verf/i"
            ".test(document.body.innerText)"
        )
        if page_gone:
            raise DMRestrictedError(f"Profile @{username} not found or unavailable")
        # Profili morti a volte vengono REDIRETTI (es. al feed home) invece di
        # mostrare l'errore: se l'URL non contiene più lo username non siamo
        # sul profilo — fermarsi qui, non cercare bottoni sul feed.
        if username.lower() not in page.url.lower():
            raise DMRestrictedError(
                f"Profile @{username} unavailable — IG redirected to {page.url}"
            )

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

        # Chiudi un eventuale dialog che copre il profilo (es. modale 'Link' dei
        # link-in-bio aperto da un tap accidentale) prima di cercare Messaggio.
        await self._dismiss_blocking_dialog(page, username)

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

        # Find DM input — una sola attesa sull'unione dei selettori (no timeout
        # impilati per locale: vedi _locate_dm_input).
        msg_input, found_selector = await self._locate_dm_input(page)
        # Anti-misinstradamento: verifica che il thread aperto sia DAVVERO del
        # bersaglio prima di scrivere (vedi _verify_dm_thread).
        thread_ok = msg_input is not None and await self._verify_dm_thread(page, username)

        if msg_input is None or not thread_ok:
            # Recovery incondizionato (una volta): input mancante OPPURE thread
            # sbagliato aperto (click 'Messaggio' atterrato sull'inbox con un
            # altro thread in cima). In entrambi i casi tornare al profilo con
            # goto e ripartire è più robusto che riconoscere ogni singolo stato.
            # (Casi reali: highlights viewer aperto da un click impreciso; e 26
            # DM finiti nel thread di @giovanni1927 perché il thread non veniva
            # verificato.)
            reason = "DM input non trovato" if msg_input is None else "thread aperto NON suo"
            logger.info(
                f"@{username}: {reason} (URL: {page.url}) — "
                f"torno al profilo e riprovo una volta"
            )
            await self._dismiss_stories_if_open(page, username)
            await page.goto(profile_url, wait_until="domcontentloaded")
            await asyncio.sleep(random.uniform(1.5, 2.5))
            await self._dismiss_ig_modals(page, username)
            await self._dismiss_blocking_dialog(page, username)
            await self._click_message_button(page, username)
            try:
                await page.wait_for_url(lambda url: '/direct/' in url, timeout=8000)
                navigated_to_direct = True
                logger.debug(f"@{username}: navigato al thread DM ({page.url})")
            except Exception:
                logger.debug(f"@{username}: retry DM click did not navigate - checking modal (URL: {page.url})")
            await asyncio.sleep(random.uniform(2.5, 4.0))
            await self._dismiss_ig_modals(page, username)

            msg_input, found_selector = await self._locate_dm_input(page)
            thread_ok = msg_input is not None and await self._verify_dm_thread(page, username)

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

        # Hard gate anti-misinstradamento: l'input c'è ma il thread aperto NON
        # risulta del bersaglio (nemmeno dopo il retry). NON scrivere: meglio un
        # mancato invio (retry pulito, il chiamante non marca 'sent') che un DM
        # consegnato alla persona sbagliata con il lead segnato 'sent' ma mai
        # contattato. Vedi _verify_dm_thread.
        if not thread_ok:
            try:
                path = f"data/debug_wrong_thread_{username}.png"
                await page.screenshot(path=path)
                from app.services.notifier import send_telegram_photo
                asyncio.create_task(send_telegram_photo(
                    path,
                    caption=(
                        f"Critical DM error: thread aperto NON di @{username} — "
                        f"invio annullato per non scrivere alla persona sbagliata"
                    ),
                    level="error",
                ))
                logger.warning(
                    f"@{username}: thread non verificato → data/debug_wrong_thread_{username}.png"
                )
            except Exception:
                pass
            raise DMSendError(
                f"@{username}: thread DM aperto non appartiene al bersaglio "
                f"(URL: {page.url}) — invio annullato (anti-misinstradamento)"
            )

        logger.debug(f"@{username}: input trovato — selettore: {found_selector}")

        # Small extra pause so the input is fully interactive before typing
        await asyncio.sleep(random.uniform(0.5, 1.0))

        # Normalizza CRLF ma PRESERVA gli a-capo: _human_type li batte come
        # Shift+Enter (Enter da solo invierebbe il DM). Collassa 3+ righe vuote.
        import re as _re
        message = message.replace('\r\n', '\n').replace('\r', '\n')
        message = _re.sub(r'\n{3,}', '\n\n', message).strip()

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

        # Send by pressing Enter — PUNTO DI NON RITORNO: da qui il DM e' partito.
        await page.keyboard.press("Enter")

        # Marca 'sending' ADESSO (non prima): qualunque fallimento sopra ha lasciato
        # il messaggio in 'message_generated' -> retry pulito, mai 'sending'. La
        # callback fa un commit DB; se fallisce non e' fatale (il DM e' gia' partito
        # e il chiamante marchera' 'sent' al ritorno di send_dm).
        if on_enter is not None:
            try:
                await on_enter()
            except Exception as e:
                logger.warning(f"@{username}: on_enter callback fallita (non-fatale): {e}")

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

        # Each session has a random base typing speed, scaled by per-account timing multiplier.
        # Tarato su un utente "digitale" (~100 WPM di picco): 40-95 ms/char.
        # La varianza lognormale + pause/typo sotto tengono il risultato umano.
        base_ms = random.uniform(40, 95) * self._tm

        # Su IG web Enter invia il DM: gli a-capo si battono come Shift+Enter
        # (newline senza invio). Tipiamo riga per riga, parola per parola.
        lines = text.split('\n')
        for line_idx, line in enumerate(lines):
            if line_idx > 0:
                # A-capo umano: Shift+Enter (non invia)
                await page.keyboard.press("Shift+Enter")
                await asyncio.sleep(random.uniform(0.15, 0.5))

            words = line.split(' ')
            for i, word in enumerate(words):
                # Occasional thinking pause before a word (more likely mid-sentence)
                if i > 0 and random.random() < 0.07:
                    await asyncio.sleep(random.uniform(0.25, 1.0))

                for char_idx, char in enumerate(word):
                    # Typo: ~8% chance per char in words >3 letters (skip first/last char)
                    if len(word) > 3 and 0 < char_idx < len(word) - 1 and random.random() < 0.08:
                        wrong = _typo_char(char)
                        if wrong:
                            err_delay = random.lognormvariate(math.log(base_ms), 0.45)
                            await page.keyboard.type(wrong)
                            await asyncio.sleep(max(30, min(480, err_delay)) / 1000)
                            await asyncio.sleep(random.uniform(0.12, 0.40))  # notice mistake
                            await page.keyboard.press("Backspace")
                            await asyncio.sleep(random.uniform(0.06, 0.20))  # before retyping

                    # Correct character with lognormal delay
                    delay_ms = random.lognormvariate(math.log(base_ms), 0.45)
                    delay_ms = max(30, min(480, delay_ms))
                    await page.keyboard.type(char)
                    await asyncio.sleep(delay_ms / 1000)
                    # Rare micro-pause within a word (re-reading, hesitation)
                    if random.random() < 0.015:
                        await asyncio.sleep(random.uniform(0.2, 0.7))

                # Type the space between words
                if i < len(words) - 1:
                    await page.keyboard.type(' ')
                    await asyncio.sleep(random.uniform(25, 80) / 1000)

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
                # Se il viewer si e' chiuso (storie finite -> IG torna al profilo),
                # FERMATI: un tap di avanzamento ora cadrebbe sul profilo, es. sul
                # link-in-bio -> apre il modale 'Link' che poi copre il bottone
                # Messaggio e fa fallire l'invio (bug reale osservato).
                if not await self._is_stories_viewer_open(page):
                    logger.debug(f"@{username}: storie chiuse/finite — stop tap di avanzamento")
                    break
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

    async def _click_message_button(self, page, username: str, _retry: bool = False) -> None:
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

        # 2a. Find and click the three dots button.
        # IMPORTANTE: scoped a `main header` (i "..." del profilo, accanto allo
        # username) — i selettori nudi matchavano anche i "..." dei POST del
        # feed: su un redirect al feed il bot apriva il menu Segnala/Non mi
        # interessa di un post a caso (caso reale osservato via screenshot).
        three_dots = (
            page.locator('main header [aria-label="Options"]')
            .or_(page.locator('main header [aria-label="More options"]'))
            .or_(page.locator('main header [aria-label="Opzioni"]'))
            .or_(page.locator('main header [aria-label="Altre opzioni"]'))
            .or_(page.locator('main header [aria-label="Altro"]'))
            .or_(page.locator('main header [aria-label="More"]'))
            .or_(page.locator('main header div[role="button"]:has(svg[aria-label="Options"])'))
            .or_(page.locator('main header div[role="button"]:has(svg[aria-label="Altre opzioni"])'))
            .or_(page.locator('main header button:has(svg[aria-label="Options"])'))
            .or_(page.locator('main header button:has(svg[aria-label="Altre opzioni"])'))
            # Fallback se IG togliesse <header> dal profilo (come per il bottone
            # Messaggio): section del profilo, mai dentro un <article> (post).
            .or_(page.locator('main section:not(:has(article)) [aria-label="Options"]'))
            .or_(page.locator('main section:not(:has(article)) [aria-label="Opzioni"]'))
            .or_(page.locator('main section:not(:has(article)) [aria-label="Altre opzioni"]'))
        )

        try:
            await three_dots.first.wait_for(state="visible", timeout=4000)
        except Exception:
            # Ne' bottone ne' tre-puntini: se nel frattempo si e' aperto il viewer
            # storie/highlights (mount ritardato di un click andato storto — IG
            # mostra lo spinner sul cerchio e monta il viewer secondi dopo, oltre
            # il check pre-bottone), chiudilo e rifai la ricerca UNA volta.
            # Caso reale @antonellonigro: viewer montato durante la ricerca ->
            # falso "User has DM restrictions".
            if not _retry and await self._is_stories_viewer_open(page):
                logger.info(
                    f"@{username}: viewer storie aperto durante la ricerca del "
                    f"bottone Messaggio — chiudo e riprovo una volta"
                )
                await self._dismiss_stories_if_open(page, username)
                return await self._click_message_button(page, username, _retry=True)
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

    # ── Feed browsing (ambient activity) ───────────────────────────────────────

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

    async def browse_reels(
        self, n_reels: int, dwell_min_s: float = 0.0, dwell_max_s: float = 10.0
    ) -> None:
        """
        ACTIVE break on the Reels feed: used between bio-scraping profiles instead
        of standing still (the old stationary "distraction" pause). Navigate to
        /reels/, then WATCH `n_reels` reels one after another — dwell a random
        `dwell_min_s..dwell_max_s` on each, then scroll DECISIVELY to the next.
        Twin of `browse_feed`, different surface.

        Advancing to the next reel (the fix): the reels feed is a full-screen
        vertical scroll-snap container. A wheel event only advances it if the
        pointer is OVER the reel — the previous version scrolled at the default
        (0,0) position (the left nav rail), so it never left the first reel. Here
        we center the pointer once, then advance with a full-viewport wheel, with
        a keyboard ArrowDown as fallback.

        Deliberately NEVER touches stories/highlights: viewing a story leaves a
        "seen" receipt visible to the target account, unlike feed/reels scrolling
        or likes — so ambient browsing must stay off that surface entirely.

        Fully defensive: any failure anywhere (navigation, DOM changed, closed
        page...) is swallowed and falls back to a plain `asyncio.sleep`, so the
        caller's per-profile cadence in the bio-scraping loop is never broken.
        """
        n_reels = max(0, int(n_reels))
        # Fallback pause ≈ what the reel session would have lasted, so a navigation
        # failure still burns comparable time instead of returning instantly.
        fallback_s = min(120.0, max(1.0, n_reels) * random.uniform(dwell_min_s, dwell_max_s))

        try:
            page = await self._get_page()
        except Exception as e:
            logger.warning(f"browse_reels: cannot get page ({e}), falling back to sleep")
            await asyncio.sleep(fallback_s)
            return

        try:
            # Navigate to Reels — prefer clicking the nav link (more human than
            # typing a URL); fall back to a direct goto if the link isn't there.
            clicked = False
            try:
                reels_nav = page.locator('a[href="/reels/"]').first
                if await reels_nav.count() > 0:
                    await reels_nav.click(timeout=2500)
                    await asyncio.sleep(random.uniform(1.5, 3.0))
                    clicked = True
            except Exception:
                pass
            if not clicked:
                await page.goto(self.BASE_URL + "/reels/", wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(1.8, 3.2))

            try:
                await self._dismiss_ig_modals(page, "reels")
            except Exception:
                pass

            # Center the pointer over the reel so wheel events target the feed
            # (not the sidebar). Done once — the pointer stays put between scrolls.
            try:
                vp = page.viewport_size or {"width": 1280, "height": 900}
            except Exception:
                vp = {"width": 1280, "height": 900}
            cx, cy = vp["width"] // 2, vp["height"] // 2
            try:
                await page.mouse.move(cx, cy, steps=random.randint(4, 10))
            except Exception:
                pass

            logger.info(f"[Ambient] Reels browse: {n_reels} reel")

            reels_viewed = 0
            for _ in range(n_reels):
                # Watch the current reel a random beat, THEN advance to the next.
                await asyncio.sleep(random.uniform(dwell_min_s, dwell_max_s))
                # Advance one reel: a full-viewport wheel at center = next snap unit.
                # Nessun like: guardare i reel (scroll + sosta) e' gia' attivita'
                # credibile e non lascia tracce.
                try:
                    await page.mouse.wheel(0, int(vp["height"] * random.uniform(0.9, 1.2)))
                except Exception:
                    # Fallback: IG reels web navigates with ArrowDown.
                    try:
                        await page.keyboard.press("ArrowDown")
                    except Exception:
                        pass
                reels_viewed += 1
                await asyncio.sleep(random.uniform(0.2, 0.6))

            logger.info(
                f"[Ambient] Reels browse done — viewed {reels_viewed} reels"
            )
        except Exception as e:
            logger.warning(
                f"[Ambient] Reels browse failed ({type(e).__name__}: {e}) — falling back to sleep"
            )
            await asyncio.sleep(fallback_s)
