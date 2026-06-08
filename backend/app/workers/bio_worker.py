from loguru import logger

from app.services.scrape_bios import scrape_bios


async def scrape_bios_task(ctx: dict, campaign_id: str) -> None:
    """ARQ task: Fase Bio (estrazione bio/contatti dai pending)."""
    logger.info(f"[ARQ] scrape_bios_task started for campaign {campaign_id}")
    try:
        await scrape_bios(campaign_id)
        logger.info(f"[ARQ] scrape_bios_task completed for campaign {campaign_id}")
    except Exception as e:
        logger.exception(f"[ARQ] scrape_bios_task failed for campaign {campaign_id}: {e}")
        raise
