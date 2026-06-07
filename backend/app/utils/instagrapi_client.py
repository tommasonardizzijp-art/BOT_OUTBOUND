"""
Shared instagrapi login utility.

Per-account asyncio lock prevents scraper and reply_checker from
simultaneously restoring the same Instagram session. Concurrent logins
on the same account can trigger challenge detection on Instagram's side,
especially during long scraping runs (>1h) that overlap with the 30-min
reply_checker cron.
"""
import asyncio
import json
from datetime import datetime
from loguru import logger

from app.models.account import InstagramAccount, AccountStatus
from app.utils.exceptions import ScraperError, AccountChallengeError


def _patch_media_xma() -> None:
    """Make MediaXma.video_url optional.

    Instagram sometimes returns null for video_url inside XMA media (reels,
    stories, link previews). instagrapi declares the field as required HttpUrl,
    so pydantic raises ValidationError and direct_threads() crashes for any
    account whose inbox contains such a thread.

    Patching at module load time (before any Client is created) covers all
    callers — recovery_checker, reply_checker, scraper.

    Strategy: create a subclass with the Optional field, inject it back into
    instagrapi.types, then rebuild DirectMessage/ReplyMessage (which reference
    MediaXma in their xma_share field) so their compiled validators pick up the
    new type.
    """
    try:
        from typing import Optional
        from pydantic import HttpUrl
        import instagrapi.types as _t

        if not hasattr(_t, "MediaXma"):
            return

        orig_field = _t.MediaXma.model_fields.get("video_url")
        if orig_field is None or not orig_field.is_required():
            return  # already optional — nothing to do

        # Build a drop-in replacement with Optional video_url.
        # Subclassing ensures all other fields and validators are preserved.
        class _PatchedMediaXma(_t.MediaXma):
            video_url: Optional[HttpUrl] = None  # type: ignore[assignment]

        _PatchedMediaXma.__name__ = "MediaXma"
        _PatchedMediaXma.__qualname__ = "MediaXma"

        # Inject into instagrapi.types so any code that imports the name picks
        # up the patched version.
        _t.MediaXma = _PatchedMediaXma

        # Rebuild dependent models (DirectMessage, ReplyMessage) so their
        # compiled validators reference the patched class.
        ns = vars(_t)
        for model_name in ("DirectMessage", "ReplyMessage"):
            model = ns.get(model_name)
            if model is None:
                continue
            fi = model.model_fields.get("xma_share")
            if fi is not None:
                fi.annotation = Optional[_PatchedMediaXma]
                model.__annotations__["xma_share"] = Optional[_PatchedMediaXma]
            model.model_rebuild(force=True, _types_namespace=ns)

        logger.debug("[Patch] instagrapi MediaXma.video_url → Optional[HttpUrl] (subclass + rebuild)")
    except Exception as exc:
        logger.warning(f"[Patch] MediaXma patch skipped (non-fatal): {exc}")


_patch_media_xma()

# Per-account lock: prevents concurrent session restores for same account
_account_locks: dict[str, asyncio.Lock] = {}
_locks_dict_mutex = asyncio.Lock()

# Scraping slot: prevents two ARQ jobs scraping with the same account concurrently.
# Risk without this: 2x API call rate on same session + race condition on session_data writes.
_scraping_accounts: set[str] = set()
_scraping_set_lock = asyncio.Lock()


async def acquire_scraping_slot(account_id: str) -> bool:
    """Claim account for scraping. Returns False if already claimed by another job."""
    async with _scraping_set_lock:
        if account_id in _scraping_accounts:
            return False
        _scraping_accounts.add(account_id)
        return True


async def release_scraping_slot(account_id: str) -> None:
    async with _scraping_set_lock:
        _scraping_accounts.discard(account_id)


def get_scraping_account_ids() -> frozenset[str]:
    """Snapshot of accounts currently in use for scraping."""
    return frozenset(_scraping_accounts)


async def _get_account_lock(account_id: str) -> asyncio.Lock:
    async with _locks_dict_mutex:
        if account_id not in _account_locks:
            _account_locks[account_id] = asyncio.Lock()
        return _account_locks[account_id]


async def login(account: InstagramAccount, db, skip_gql_verify: bool = False) -> "Client":
    """
    Restore Instagram session for `account`.
    Acquires per-account lock — only one caller at a time can restore
    the same account's session, preventing concurrent login races.
    NEVER attempts automated fresh login (ban risk). Requires prior
    manual browser login via Account → 'Login Browser'.

    skip_gql_verify=True: skip the user_info_by_username_gql ping (reply checker).
    The GQL call is only needed before mobile scraping (web→mobile session jump),
    not for reading the DM inbox which uses a different API surface.
    """
    lock = await _get_account_lock(account.id)
    async with lock:
        return await _do_login(account, db, skip_gql_verify=skip_gql_verify)


async def _do_login(account: InstagramAccount, db, skip_gql_verify: bool = False) -> "Client":
    from instagrapi import Client

    client = Client()

    if account.proxy:
        client.set_proxy(account.proxy)

    if not account.session_data:
        raise ScraperError(
            f"@{account.username} non ha una sessione salvata. "
            "Vai su Account → 'Login Browser' per effettuare il login manuale "
            "PRIMA di avviare una campagna."
        )

    try:
        session = json.loads(account.session_data)
        client.set_settings(session)

        if not skip_gql_verify:
            # Verify session via WEB GraphQL (not mobile API).
            # Calling mobile `account_info()` right after a fresh manual web login
            # triggers UFAC challenge ("ufac_www_bloks") due to web→mobile session
            # jump. GraphQL accepts web cookies cleanly, lets the session "bake"
            # before scraper actually hits mobile endpoints.
            #
            # If this endpoint returns 429 (IP-level rate limit), skip the verify
            # and proceed — 429 here means the web endpoint is throttled, NOT that
            # the mobile session is invalid. Blocking login on a transient IP limit
            # prevents all scraping even when the account is perfectly usable.
            try:
                await asyncio.to_thread(
                    client.user_info_by_username_gql, account.username
                )
            except Exception as gql_exc:
                gql_str = str(gql_exc).lower()
                if "429" in gql_str or "too many" in gql_str or "retryerror" in type(gql_exc).__name__.lower():
                    logger.warning(
                        f"GQL verify 429 per @{account.username} — "
                        "endpoint web rate-limited, proseguo con sessione mobile"
                    )
                else:
                    raise

        account.session_data = json.dumps(client.get_settings())
        account.last_login_at = datetime.utcnow()
        await db.commit()

        label = "senza verifica GQL" if skip_gql_verify else "verifica web GQL"
        logger.info(f"Sessione ripristinata per @{account.username} ({label})")
        return client

    except Exception as e:
        # Catch ALL challenge variants (ChallengeRequired, ChallengeUnknownStep,
        # and any future step_name Instagram may add) by checking the exception
        # class name — instagrapi uses a flat hierarchy where all challenge errors
        # inherit from ClientError but their names consistently contain "Challenge".
        exc_name = type(e).__name__
        if "Challenge" in exc_name:
            account.status = AccountStatus.challenge_required
            await db.commit()
            logger.warning(
                f"Challenge rilevata per @{account.username} ({exc_name}) — "
                "account marcato challenge_required. Login manuale richiesto."
            )
            raise AccountChallengeError(account.id, str(e))

        logger.error(
            f"Ripristino sessione fallito per @{account.username}: "
            f"{exc_name}: {e}"
        )
        raise ScraperError(
            f"La sessione di @{account.username} è scaduta o non valida. "
            "Vai su Account → 'Login Browser' per rifare il login manuale. "
            f"(Errore: {exc_name})"
        )
