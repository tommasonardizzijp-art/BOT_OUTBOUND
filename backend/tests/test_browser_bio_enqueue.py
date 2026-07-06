"""Fan-out: un task ARQ per account, stagger via _defer_by, job_id deterministico."""
import pytest

from app.services import browser_bio


def test_job_id_deterministic():
    jid = browser_bio.browser_bio_job_id("camp1", "accA")
    assert jid == "biobrowser:camp1:accA"


class _FakeRedis:
    """Redis fake minimale: traccia enqueue/delete, exists configurabile per test."""
    def __init__(self, in_progress_keys=None):
        self.in_progress_keys = set(in_progress_keys or ())
        self.calls = []
        self.deleted = []
        self.closed = 0

    async def enqueue_job(self, *args, **kwargs):
        self.calls.append((args, kwargs))

    async def exists(self, key):
        return 1 if key in self.in_progress_keys else 0

    async def delete(self, *keys):
        self.deleted.extend(keys)

    async def aclose(self):
        self.closed += 1


@pytest.mark.asyncio
async def test_enqueue_one_task_per_account(monkeypatch):
    fake = _FakeRedis()

    async def fake_pool(*a, **k):
        return fake

    async def fake_accounts(campaign_id):
        return [("accA", "userA"), ("accB", "userB")]

    monkeypatch.setattr(browser_bio.arq, "create_pool", fake_pool)
    monkeypatch.setattr(browser_bio, "_scraping_accounts_of_campaign", fake_accounts)

    n = await browser_bio.enqueue_browser_bio_workers("camp1")
    assert n == 2
    assert len(fake.calls) == 2
    # primo account: nessun defer (idx 0); secondo: defer > 0
    kwargs0, kwargs1 = fake.calls[0][1], fake.calls[1][1]
    assert kwargs0["_job_id"] == "biobrowser:camp1:accA"
    assert kwargs1["_job_id"] == "biobrowser:camp1:accB"
    assert kwargs0.get("_defer_by", 0) == 0
    assert kwargs1["_defer_by"] > 0
    assert fake.closed == 1  # il pool ARQ viene chiuso (no leak)


@pytest.mark.asyncio
async def test_enqueue_skips_in_progress(monkeypatch):
    # accA ha gia' un job in-progress: non va ri-accodato (ne' le sue chiavi
    # job/retry cancellate), ma resta contato come "schedulato". accB e'
    # libero: viene accodato normalmente.
    in_progress_key = browser_bio.browser_bio_redis_keys("camp1", "accA")[2]
    fake = _FakeRedis(in_progress_keys={in_progress_key})

    async def fake_pool(*a, **k):
        return fake

    async def fake_accounts(campaign_id):
        return [("accA", "userA"), ("accB", "userB")]

    monkeypatch.setattr(browser_bio.arq, "create_pool", fake_pool)
    monkeypatch.setattr(browser_bio, "_scraping_accounts_of_campaign", fake_accounts)

    n = await browser_bio.enqueue_browser_bio_workers("camp1")
    assert n == 2  # 1 in-progress (skip) + 1 accodato
    assert len(fake.calls) == 1
    assert fake.calls[0][1]["_job_id"] == "biobrowser:camp1:accB"
    # nessuna delete sulle chiavi dell'account in-progress
    assert not any("accA" in k for k in fake.deleted)
    assert fake.closed == 1
