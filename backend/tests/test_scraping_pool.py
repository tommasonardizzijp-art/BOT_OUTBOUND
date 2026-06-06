"""Unit + integration tests per il round-robin multi-account scraping (Approccio C)."""
from types import SimpleNamespace

import pytest

from app.services.scraping_pool import ScrapingPool


def _entry(account_id, lookups, client):
    acct = SimpleNamespace(id=account_id, username=account_id, scrape_lookups_today=lookups)
    return {"account": acct, "client": client, "slot_owned": True}


def _campaign(limit=180):
    # has_scrape_budget legge scrape_daily_limit_for(account, campaign):
    # campaign.scrape_daily_limit override, altrimenti settings globale.
    return SimpleNamespace(scrape_daily_limit=limit)


class TestScrapingPoolNext:
    def test_alternates_between_two_accounts(self):
        pool = ScrapingPool([_entry("A", 0, "clientA"), _entry("B", 0, "clientB")])
        camp = _campaign()
        seq = [pool.next(camp)[0].id for _ in range(4)]
        assert seq == ["A", "B", "A", "B"]

    def test_single_account_always_same(self):
        pool = ScrapingPool([_entry("A", 0, "clientA")])
        camp = _campaign()
        assert [pool.next(camp)[0].id for _ in range(3)] == ["A", "A", "A"]

    def test_skips_capped_account(self):
        # B è a cap (180/180) → deve restituire sempre A
        pool = ScrapingPool([_entry("A", 0, "clientA"), _entry("B", 180, "clientB")])
        camp = _campaign(limit=180)
        assert [pool.next(camp)[0].id for _ in range(3)] == ["A", "A", "A"]

    def test_returns_none_when_all_capped(self):
        pool = ScrapingPool([_entry("A", 180, "clientA"), _entry("B", 180, "clientB")])
        camp = _campaign(limit=180)
        assert pool.next(camp) is None

    def test_returns_client_with_account(self):
        pool = ScrapingPool([_entry("A", 0, "clientA")])
        acct, client = pool.next(_campaign())
        assert acct.id == "A" and client == "clientA"

    def test_empty_pool_returns_none(self):
        assert ScrapingPool([]).next(_campaign()) is None
