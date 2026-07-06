"""Fan-out: un task ARQ per account, stagger via _defer_by, job_id deterministico."""
import pytest

from app.services import browser_bio


def test_job_id_deterministic():
    jid = browser_bio.browser_bio_job_id("camp1", "accA")
    assert jid == "biobrowser:camp1:accA"


@pytest.mark.asyncio
async def test_enqueue_one_task_per_account(monkeypatch):
    calls = []

    class _FakeRedis:
        async def enqueue_job(self, *args, **kwargs):
            calls.append((args, kwargs))

    async def fake_pool(*a, **k):
        return _FakeRedis()

    async def fake_accounts(campaign_id):
        return [("accA", "userA"), ("accB", "userB")]

    monkeypatch.setattr(browser_bio.arq, "create_pool", fake_pool)
    monkeypatch.setattr(browser_bio, "_scraping_accounts_of_campaign", fake_accounts)

    n = await browser_bio.enqueue_browser_bio_workers("camp1")
    assert n == 2
    assert len(calls) == 2
    # primo account: nessun defer (idx 0); secondo: defer > 0
    kwargs0, kwargs1 = calls[0][1], calls[1][1]
    assert kwargs0["_job_id"] == "biobrowser:camp1:accA"
    assert kwargs1["_job_id"] == "biobrowser:camp1:accB"
    assert kwargs0.get("_defer_by", 0) == 0
    assert kwargs1["_defer_by"] > 0
