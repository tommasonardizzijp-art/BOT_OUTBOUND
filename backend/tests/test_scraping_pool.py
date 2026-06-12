"""Unit + integration tests per il round-robin multi-account scraping (Approccio C)."""
from types import SimpleNamespace

import pytest

from app.services.scraping_pool import ScrapingPool


def _entry(account_id, lookups, client):
    # scrape_lookups_date = oggi: il conteggio rappresenta uso ODIERNO (altrimenti
    # il reset lazy lo tratterebbe come stale=0 e nessun account risulterebbe a cap).
    from datetime import datetime
    acct = SimpleNamespace(
        id=account_id, username=account_id, scrape_lookups_today=lookups,
        scrape_lookups_date=datetime.utcnow().strftime("%Y-%m-%d"),
    )
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


class TestStoreFollowersRoundRobin:
    """_store_followers_batch deve alternare gli account del pool per-lead."""

    @pytest.mark.asyncio
    async def test_user_info_alternates_accounts(self, monkeypatch):
        import app.services.scraper as scraper
        from unittest.mock import AsyncMock, MagicMock

        # follower shorts da scrapare (pk diversi)
        shorts = []
        for i in range(4):
            s = SimpleNamespace(
                pk=str(1000 + i), username=f"u{i}", full_name=f"U{i}",
                is_private=False, profile_pic_url=None,
            )
            shorts.append(s)

        # due client mock con user_info_v1 che ritorna un oggetto bio minimale
        def make_client(tag):
            c = MagicMock(name=f"client-{tag}")
            info = SimpleNamespace(
                biography="bio", is_verified=False, follower_count=1,
                following_count=1, external_url=None,
            )
            c.user_info_v1 = MagicMock(return_value=info)
            return c
        clientA, clientB = make_client("A"), make_client("B")
        pool = ScrapingPool([_entry("A", 0, clientA), _entry("B", 0, clientB)])

        # campaign + db fake
        camp = SimpleNamespace(
            id="camp1", scrape_daily_limit=180, bio_fetch_delay_min=0, bio_fetch_delay_max=0,
            status=scraper.CampaignStatus.scraping,
        )
        db = MagicMock()
        db.refresh = AsyncMock()
        db.commit = AsyncMock()
        db.add = MagicMock()
        # nessun duplicato in DB
        exec_res = MagicMock()
        exec_res.scalar_one_or_none = MagicMock(return_value=None)
        db.execute = AsyncMock(return_value=exec_res)

        # stub delle dipendenze esterne
        monkeypatch.setattr(scraper, "is_halted", AsyncMock(return_value=False))
        monkeypatch.setattr(scraper, "increment_scrape_lookup", AsyncMock())
        monkeypatch.setattr(scraper, "extract_contacts", lambda info: scraper.ContactData())
        monkeypatch.setattr(scraper, "upsert_lead", AsyncMock())

        stored = await scraper._store_followers_batch(shorts, camp, db, pool, "followers")

        assert stored == 4
        # 4 lead, alternati A,B,A,B → 2 call per client
        assert clientA.user_info_v1.call_count == 2
        assert clientB.user_info_v1.call_count == 2
