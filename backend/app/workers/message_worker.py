from loguru import logger
from arq.worker import Retry
from app.services.campaign_orchestrator import run_campaign_worker


async def run_campaign_task(ctx: dict, campaign_id: str, account_id: str) -> None:
    """
    ARQ task: run the single-account campaign worker loop.

    One task is enqueued per assigned account when a campaign starts.
    Workers are independent — they compete for followers via optimistic locking.
    """
    logger.info(f"[ARQ] run_campaign_task started — campaign={campaign_id}, account={account_id}")
    try:
        await run_campaign_worker(campaign_id, account_id)
        logger.info(f"[ARQ] run_campaign_task finished — campaign={campaign_id}, account={account_id}")
    except Retry:
        logger.info(f"[ARQ] run_campaign_task deferred - campaign={campaign_id}, account={account_id}")
        raise
    except Exception as e:
        logger.exception(
            f"[ARQ] run_campaign_task failed — campaign={campaign_id}, account={account_id}: {e}"
        )
        raise
