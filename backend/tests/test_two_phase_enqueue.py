"""Two-phase: enqueue helpers usano job-id dedicati e funzioni giuste."""
import pytest
from app.services.work_enqueue import _enqueue_list_with_redis, _enqueue_bios_with_redis


class _FakeRedis:
    def __init__(self, in_progress: set[str] | None = None):
        self.enqueued = []
        self.deleted = []
        self._in_progress = in_progress or set()

    async def exists(self, key):
        return 1 if key in self._in_progress else 0

    async def delete(self, *keys):
        self.deleted.extend(keys)

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


@pytest.mark.asyncio
async def test_enqueue_bios_skips_when_in_progress():
    """Guardia concorrenza: job gia' in esecuzione => no duplicato (no KeyError arq)."""
    r = _FakeRedis(in_progress={"arq:in-progress:bios:camp-1"})
    result = await _enqueue_bios_with_redis(r, "camp-1")
    assert result is False
    assert r.enqueued == []
    # NON deve cancellare l'in-progress lock dell'altro job
    assert "arq:in-progress:bios:camp-1" not in r.deleted


@pytest.mark.asyncio
async def test_enqueue_bios_clears_parked_retry():
    """Job non in esecuzione: cancella job/retry parcheggiati e ri-accoda."""
    r = _FakeRedis()
    result = await _enqueue_bios_with_redis(r, "camp-1")
    assert result is True
    assert "arq:retry:bios:camp-1" in r.deleted
    assert "arq:in-progress:bios:camp-1" not in r.deleted
