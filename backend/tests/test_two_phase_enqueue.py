"""Two-phase: enqueue helpers usano job-id dedicati e funzioni giuste."""
import pytest
from app.services.work_enqueue import _enqueue_list_with_redis, _enqueue_bios_with_redis


class _FakeRedis:
    def __init__(self):
        self.enqueued = []

    async def delete(self, *keys):
        pass

    async def enqueue_job(self, fn, *args, **kwargs):
        self.enqueued.append((fn, args, kwargs))


@pytest.mark.asyncio
async def test_enqueue_list():
    r = _FakeRedis()
    await _enqueue_list_with_redis(r, "camp-1")
    fn, args, kwargs = r.enqueued[0]
    assert fn == "list_followers_task"
    assert args == ("camp-1",)
    assert kwargs["_job_id"] == "list:camp-1"


@pytest.mark.asyncio
async def test_enqueue_bios():
    r = _FakeRedis()
    await _enqueue_bios_with_redis(r, "camp-1")
    fn, args, kwargs = r.enqueued[0]
    assert fn == "scrape_bios_task"
    assert args == ("camp-1",)
    assert kwargs["_job_id"] == "bios:camp-1"
