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

    Strategy: mutate the EXISTING MediaXma class in place (make video_url
    optional) and rebuild it + DirectMessage/ReplyMessage. We must NOT subclass:
    instagrapi.extractors captured `from .types import MediaXma` at its own import
    time and builds instances via that reference (extract_media_v1_xma →
    MediaXma(...)). A subclass left those extractor-built instances as the ORIGINAL
    class, which then failed DirectMessage validation with
    'Input should be a valid dictionary or instance of _PatchedMediaXma'
    (type-identity mismatch) — crashing direct_threads() for ANY thread containing
    an xma_share, video_url null or not. Mutating in place keeps a single class
    object shared by types + extractors, so every instance validates.
    """
    try:
        from typing import Optional
        from pydantic import HttpUrl
        from pydantic_core import PydanticUndefined
        import instagrapi.types as _t

        if not hasattr(_t, "MediaXma"):
            return

        field = _t.MediaXma.model_fields.get("video_url")
        if field is None or not field.is_required():
            return  # already optional — nothing to do

        # Mutate the field on the original class: Optional annotation + None default
        # (default != PydanticUndefined ⇒ is_required() becomes False).
        field.annotation = Optional[HttpUrl]
        field.default = None
        _t.MediaXma.model_rebuild(force=True)

        # Rebuild dependent models so their compiled xma_share validators pick up
        # MediaXma's new (None-tolerant) core schema. Annotation stays the same
        # class object — only its schema changed.
        ns = vars(_t)
        for model_name in ("DirectMessage", "ReplyMessage"):
            model = ns.get(model_name)
            if model is not None:
                model.model_rebuild(force=True, _types_namespace=ns)

        logger.debug("[Patch] instagrapi MediaXma.video_url → Optional[HttpUrl] (in-place mutate + rebuild)")
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

        # Sessions saved before the header-auth fix lack this flag, which
        # instagrapi's canonical login_by_sessionid always sets. Without it
        # Instagram 404s header-auth mobile endpoints (direct_v2/inbox →
        # "Endpoint does not exist"). Inject defensively so already-saved
        # sessions work without forcing a manual re-login; it persists on the
        # session_data re-save below (get_settings includes authorization_data).
        if isinstance(getattr(client, "authorization_data", None), dict):
            client.authorization_data.setdefault("should_use_header_over_cookies", True)

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
