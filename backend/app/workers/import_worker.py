from loguru import logger
from arq.worker import Retry

from app.services.import_resolver import resolve_imports
from app.utils.db_resilience import is_transient_db_error

DB_RETRY_DEFER_SECONDS = 60


async def resolve_imports_task(ctx: dict, campaign_id: str) -> None:
    """ARQ task: resolve imported profiles into bio_scraped Followers."""
    logger.info(f"[ARQ] resolve_imports_task started for campaign {campaign_id}")
    try:
        # Il motore browser (bio_engine='browser') lavora a mini-sessioni e ritorna i
        # secondi di defer per la pausa lunga anti-block: qui lo trasformiamo in
        # Retry(defer), come browser_bio_account_task. Il path API ritorna None.
        defer = await resolve_imports(campaign_id)
        if defer:
            logger.info(f"[ARQ] resolve_imports_task pausa browser — defer {defer}s")
            raise Retry(defer=defer)
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
