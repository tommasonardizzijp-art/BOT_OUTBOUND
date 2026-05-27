import asyncio
import functools
from loguru import logger


def async_retry(max_attempts: int = 3, base_delay: float = 2.0, exceptions: tuple = (Exception,)):
    """
    Decorator for async functions with exponential backoff retry.

    Usage:
        @async_retry(max_attempts=3, base_delay=2.0, exceptions=(httpx.HTTPError,))
        async def my_func(): ...
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt == max_attempts:
                        logger.warning(
                            f"{func.__name__} failed after {max_attempts} attempts: {exc}"
                        )
                        raise
                    delay = base_delay * (2 ** (attempt - 1))
                    logger.debug(f"{func.__name__} attempt {attempt} failed, retrying in {delay:.1f}s: {exc}")
                    await asyncio.sleep(delay)
            raise last_exc
        return wrapper
    return decorator
