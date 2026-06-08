from loguru import logger

from app.services.scrape_list import list_followers


async def list_followers_task(ctx: dict, campaign_id: str) -> None:
    """ARQ task: Fase Lista (raccolta info base follower)."""
    logger.info(f"[ARQ] list_followers_task started for campaign {campaign_id}")
    try:
        await list_followers(campaign_id)
        logger.info(f"[ARQ] list_followers_task completed for campaign {campaign_id}")
    except Exception as e:
        logger.exception(f"[ARQ] list_followers_task failed for campaign {campaign_id}: {e}")
        raise
