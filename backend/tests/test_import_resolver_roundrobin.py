"""Round-robin multi-account per il resolver import (Approccio C esteso a import mode)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.scraping_pool import ScrapingPool
import app.services.import_resolver as ir


def _entry(account_id, lookups, client):
    acct = SimpleNamespace(id=account_id, username=account_id, scrape_lookups_today=lookups)
    return {"account": acct, "client": client, "slot_owned": True}


def _campaign(limit=180):
    return SimpleNamespace(id="c", scrape_daily_limit=limit)


class TestResolveOneRotation:
    """_resolve_one ruota all'account successivo del pool su 429, senza re-login."""

    @pytest.mark.asyncio
    async def test_success_no_rotation(self, monkeypatch):
        info = SimpleNamespace(pk="5", username="x", is_private=False)
        cA = MagicMock()
        cA.user_info_by_username_v1 = MagicMock(return_value=info)
        acctA = SimpleNamespace(id="A", username="A", scrape_lookups_today=0)
        pool = ScrapingPool([_entry("A", 0, cA)])
        monkeypatch.setattr(ir.asyncio, "sleep", AsyncMock())

        got, err, used = await ir._resolve_one(None, _campaign(), "x", pool, acctA, cA)

        assert err is None and got is info and used.id == "A"
        assert cA.user_info_by_username_v1.call_count == 1

    @pytest.mark.asyncio
    async def test_rotates_to_next_pool_account_on_429(self, monkeypatch):
        infoB = SimpleNamespace(pk="9", username="y", is_private=False)
        cA = MagicMock()
        cA.user_info_by_username_v1 = MagicMock(side_effect=Exception("429 Too Many Requests"))
        cB = MagicMock()
        cB.user_info_by_username_v1 = MagicMock(return_value=infoB)
        acctA = SimpleNamespace(id="A", username="A", scrape_lookups_today=0)
        pool = ScrapingPool([_entry("A", 0, cA), _entry("B", 0, cB)])
        monkeypatch.setattr(ir.asyncio, "sleep", AsyncMock())

        # Simula la scelta per-riga del loop: pool.next() restituisce A (idx→1),
        # poi _resolve_one su 429 chiede pool.next() → B (niente re-login).
        first = pool.next(_campaign())
        assert first[0].id == "A"
        got, err, used = await ir._resolve_one(None, _campaign(), "y", pool, first[0], first[1])

        assert err is None and got is infoB and used.id == "B"
        assert cB.user_info_by_username_v1.call_count == 1
        # nessun login richiamato: i client del pool sono pre-loggati


class _FakeCtx:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *a):
        return False


def _result(value):
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    return r


class TestResolveImportsLoopRoundRobin:
    """e2e del loop resolve_imports: 4 righe pending → i 2 account si alternano A,B,A,B."""

    @pytest.mark.asyncio
    async def test_loop_alternates_accounts_per_row(self, monkeypatch):
        from app.models.campaign import CampaignStatus
        from app.utils.contact_extract import ContactData

        def make_client(tag):
            c = MagicMock(name=f"client-{tag}")
            c.get_settings = MagicMock(return_value={})
            c.user_info_by_username_v1 = MagicMock(side_effect=lambda u: SimpleNamespace(
                pk=str(abs(hash((tag, u))) % 100000), username=u, is_private=False,
                full_name=u, biography="b", is_verified=False,
                follower_count=1, following_count=1, profile_pic_url=None,
            ))
            return c

        cA, cB = make_client("A"), make_client("B")
        pool = ScrapingPool([
            {"account": SimpleNamespace(id="A", username="A", scrape_lookups_today=0,
                                        session_data=None, last_activity_at=None),
             "client": cA, "slot_owned": False},
            {"account": SimpleNamespace(id="B", username="B", scrape_lookups_today=0,
                                        session_data=None, last_activity_at=None),
             "client": cB, "slot_owned": False},
        ])

        campaign = SimpleNamespace(
            id="camp1", source_type="import", status=CampaignStatus.scraping,
            scrape_daily_limit=180, bio_fetch_delay_min=0, bio_fetch_delay_max=0,
            scrape_session_size=250, messaging_enabled=True,
            total_followers=0, messages_pending=0, scrape_outcome=None,
            scrape_completed_at=None, updated_at=None,
        )
        rows = [SimpleNamespace(username=f"u{i}", status="pending", error=None, ig_user_id=None)
                for i in range(4)]

        db = MagicMock()
        db.refresh = AsyncMock()
        db.commit = AsyncMock()
        db.add = MagicMock()
        db.scalar = AsyncMock(return_value=4)
        # ordine execute: Campaign, reset 'resolving'->'pending' (path API), poi
        # (pending-row, follower-dup) ×4, poi pending=None
        db.execute = AsyncMock(side_effect=[
            _result(campaign),
            _result(None),  # UPDATE reset ImportedProfile 'resolving'->'pending' (ritorno ignorato)
            _result(rows[0]), _result(None),
            _result(rows[1]), _result(None),
            _result(rows[2]), _result(None),
            _result(rows[3]), _result(None),
            _result(None),
        ])

        monkeypatch.setattr(ir, "AsyncSessionLocal", lambda: _FakeCtx(db))
        monkeypatch.setattr(ir, "is_halted", AsyncMock(return_value=False))
        monkeypatch.setattr(ir, "increment_scrape_lookup", AsyncMock())
        monkeypatch.setattr(ir, "extract_contacts", lambda info: ContactData())
        monkeypatch.setattr(ir, "upsert_lead", AsyncMock())
        monkeypatch.setattr(ir.asyncio, "sleep", AsyncMock())
        monkeypatch.setattr(ir.ScrapingPool, "build", AsyncMock(return_value=pool))

        await ir.resolve_imports("camp1")

        # 4 righe alternate A,B,A,B → 2 call per client
        assert cA.user_info_by_username_v1.call_count == 2
        assert cB.user_info_by_username_v1.call_count == 2
        # tutte le righe risolte
        assert all(r.status == "resolved" for r in rows)
        # campagna chiusa in ready (messaging_enabled=True)
        assert campaign.status == CampaignStatus.ready
