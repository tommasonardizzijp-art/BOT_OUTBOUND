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
            await asyncio.to_thread(
                client.user_info_by_username_gql, account.username
            )

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
