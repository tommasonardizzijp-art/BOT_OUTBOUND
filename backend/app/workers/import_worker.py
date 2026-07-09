from loguru import logger
from arq.worker import Retry

from app.services.import_resolver import resolve_imports
from app.utils.db_resilience import is_transient_db_error

DB_RETRY_DEFER_SECONDS = 60


async def resolve_imports_task(ctx: dict, campaign_id: str) -> None:
    """ARQ task: resolve imported profiles into bio_scraped Followers."""
    logger.info(f"[ARQ] resolve_imports_task started for campaign {campaign_id}")
    try:
        # Per bio_engine='browser' questo fa solo il fan-out (accoda i worker per-account)
        # ed esce; le pause lunghe stanno nei browser_import_account_task. Path API: inline.
        await resolve_imports(campaign_id)
        logger.info(f"[ARQ] resolve_imports_task completed for campaign {campaign_id}")
    except Retry:
        raise
    except Exception as e:
        if is_transient_db_error(e):
            logger.warning(
                f"[ARQ] resolve_imports_task transient DB/network error — defer {DB_RETRY_DEFER_SECONDS}s "
                f"campaign {campaign_id}: {e}"
            )
            raise Retry(defer=DB_RETRY_DEFER_SECONDS)
        logger.exception(f"[ARQ] resolve_imports_task failed for {campaign_id}: {e}")
        raise
