from arq import ArqRedis
from loguru import logger
from app.services.scraper import scrape_followers


async def scrape_followers_task(ctx: dict, campaign_id: str) -> None:
    """ARQ task: scrape all followers for the given campaign."""
    logger.info(f"[ARQ] scrape_followers_task started for campaign {campaign_id}")
    try:
        await scrape_followers(campaign_id)
        logger.info(f"[ARQ] scrape_followers_task completed for campaign {campaign_id}")
    except Exception as e:
        logger.exception(f"[ARQ] scrape_followers_task failed for campaign {campaign_id}: {e}")
        raise
