from loguru import logger
from arq.worker import Retry

from app.services.scrape_bios import scrape_bios
from app.utils.db_resilience import is_transient_db_error

DB_RETRY_DEFER_SECONDS = 60


async def scrape_bios_task(ctx: dict, campaign_id: str) -> None:
    """ARQ task: Fase Bio (estrazione bio/contatti dai pending)."""
    logger.info(f"[ARQ] scrape_bios_task started for campaign {campaign_id}")
    try:
        defer = await scrape_bios(campaign_id)
        if defer:
            logger.info(f"[ARQ] scrape_bios_task pausa sessione — defer {defer}s campaign {campaign_id}")
            raise Retry(defer=defer)
        logger.info(f"[ARQ] scrape_bios_task completed for campaign {campaign_id}")
    except Retry:
        raise
    except Exception as e:
        if is_transient_db_error(e):
            logger.warning(
                f"[ARQ] scrape_bios_task transient DB/network error — defer {DB_RETRY_DEFER_SECONDS}s "
                f"campaign {campaign_id}: {e}"
            )
            raise Retry(defer=DB_RETRY_DEFER_SECONDS)
        logger.exception(f"[ARQ] scrape_bios_task failed for campaign {campaign_id}: {e}")
        raise
