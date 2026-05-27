"""
DM Sender — sends Instagram DMs via Patchright (undetected Playwright fork).

This service is the bridge between the campaign orchestrator and the browser.
It manages the Patchright browser context for each account and uses
human-like interaction patterns.

NOTE: Patchright must be installed separately:
  pip install patchright
  patchright install chromium
"""
from loguru import logger
from app.utils.exceptions import DMSendError, DMRestrictedError, AccountBannedError, AccountChallengeError


async def send_dm(account_id: str, username: str, message_text: str) -> None:
    """
    Send a DM to `username` from account `account_id`.
    Uses a persistent browser profile for the account.

    Raises:
        DMSendError: Generic send failure
        DMRestrictedError: Target has DM restrictions
        AccountBannedError: Account detected as banned
        AccountChallengeError: Instagram requires security verification
    """
    try:
        from app.browser.context_manager import get_browser_context
        from app.browser.instagram_page import InstagramPage
    except ImportError:
        raise DMSendError(
            "Patchright non installato. Esegui: pip install patchright && patchright install chromium"
        )

    from app.browser.fingerprint import get_fingerprint
    from app.database import AsyncSessionLocal
    from app.models.account import InstagramAccount
    from sqlalchemy import select
    from datetime import datetime

    timing_multiplier = get_fingerprint(account_id).get("timing_multiplier", 1.0)

    # Determine if this account needs extended pre-DM browsing (fresh / low-warmup).
    # Fresh = age < 7 days OR warmup_day in 1..6.
    extended_browse = False
    async with AsyncSessionLocal() as db:
        r = await db.execute(select(InstagramAccount).where(InstagramAccount.id == account_id))
        acc = r.scalar_one_or_none()
        if acc:
            age_days = (datetime.utcnow() - acc.created_at).days if acc.created_at else 999
            if age_days < 7 or (1 <= acc.warmup_day <= 6):
                extended_browse = True

    async with get_browser_context(account_id) as context:
        page = InstagramPage(context, timing_multiplier=timing_multiplier, extended_browse=extended_browse)
        await page.ensure_logged_in(account_id)
        await page.send_dm(username=username, message=message_text)
