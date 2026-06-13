"""Regression: the collection dispatcher must route import → resolve, scrape → scrape.

Guards against the audit bloccante where resume/reenqueue ran scrape_followers_task
on import campaigns (target_username=None) and errored them out.
"""
import pytest
from app.services.work_enqueue import _enqueue_collection_with_redis


class _FakeRedis:
    def __init__(self):
        self.enqueued = []

    async def exists(self, key):
        # Nessun job in-progress nei test: _reenqueue_phase deve poter accodare.
        return 0

    async def delete(self, *keys):
        pass

    async def enqueue_job(self, fn, *args, **kwargs):
        self.enqueued.append((fn, args, kwargs))


@pytest.mark.asyncio
async def test_import_campaign_enqueues_resolve():
    r = _FakeRedis()
    await _enqueue_collection_with_redis(r, "camp-import", "import")
    fn, args, kwargs = r.enqueued[0]
    assert fn == "resolve_imports_task"
    assert args == ("camp-import",)
    assert kwargs["_job_id"] == "resolve:camp-import"


@pytest.mark.asyncio
async def test_scrape_campaign_enqueues_scrape():
    r = _FakeRedis()
    await _enqueue_collection_with_redis(r, "camp-scrape", "scrape")
    fn, args, kwargs = r.enqueued[0]
    assert fn == "scrape_followers_task"
    assert args == ("camp-scrape",)
    assert kwargs["_job_id"] == "scrape:camp-scrape"
