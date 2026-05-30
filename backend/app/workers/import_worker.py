from loguru import logger
from app.services.import_resolver import resolve_imports


async def resolve_imports_task(ctx: dict, campaign_id: str) -> None:
    """ARQ task: resolve imported profiles into bio_scraped Followers."""
    logger.info(f"[ARQ] resolve_imports_task started for campaign {campaign_id}")
    try:
        await resolve_imports(campaign_id)
        logger.info(f"[ARQ] resolve_imports_task completed for campaign {campaign_id}")
    except Exception as e:
        logger.exception(f"[ARQ] resolve_imports_task failed for {campaign_id}: {e}")
        raise
