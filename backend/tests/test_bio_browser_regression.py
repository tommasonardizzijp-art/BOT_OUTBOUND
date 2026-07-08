"""Regressione — due bug adversarial-confermati su `scrape_bios_browser_session`
(branch feat/bio-scraping-browser-mode), consolidati da tests/test_advq_session.py
e tests/test_advq_e2e.py (rimossi come scratch dopo il fix):

1) BUG 1 (Important): i rami di rilascio-lock (`not_found/private/error`,
   `soft_block`, `network`, outcome inatteso) facevano `db.commit()` sulla STESSA
   AsyncSession usata da `fetch_and_store_bio_browser`. Se il commit di `fetch`
   falliva a monte (errore transitorio Postgres/Supabase su flush), la sessione
   restava in stato PendingRollback: anche il commit del ramo di rilascio falliva,
   propagava all'except esterno (`return 300`) e il follower restava
   pending+LOCKED (il lock era gia' stato committato dal claim) fino al cron di
   stale-lock (20 min). Fix: `_resilient_release` fa un rollback preventivo +
   UPDATE per id (non dipende dall'oggetto ORM, espirato dopo il rollback).

2) BUG 2 (Minor): il ramo `done_count >= cap` ritornava sempre la pausa lunga
   anti-block (30-45 min) senza controllare se il pool fosse nel frattempo vuoto.
   Se un solo account drena l'ULTIMO pending esattamente al cap, il loop esce da
   qui (non da `claim -> None`), quindi `_maybe_complete_browser_bio` non veniva
   mai chiamato e la campagna restava 'scraping' con 0 pending per l'intera pausa.
   Fix: tentare il completamento PRIMA di pausare.
"""
from datetime import datetime

import pytest
from sqlalchemy import select, func

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.services import browser_bio


class _FakeSession:
    def __init__(self, *a, **k):
        self.opened = False
        self.closed = False

    async def open(self):
        self.opened = True

    async def close(self):
        self.closed = True

    class _P:
        async def ensure_logged_in(self, account_id, allow_login=True):
            return None

    page = _P()


async def _anoop():
    return None


async def _anoop_false():
    return False


def _patch_common(monkeypatch, cap):
    monkeypatch.setattr(browser_bio, "BrowserSession", _FakeSession)
    monkeypatch.setattr(browser_bio, "pick_session_cap", lambda *a, **k: cap)
    monkeypatch.setattr(browser_bio, "human_profile_pause", lambda: _anoop())
    monkeypatch.setattr(browser_bio, "maybe_micro_scroll", lambda *a, **k: _anoop_false())


@pytest.mark.asyncio
async def test_release_resilient_to_poisoned_session(monkeypatch):
    """BUG 1: fake `fetch_and_store_bio_browser` che scrive dei campi e poi fa
    `follower.username = None; await db.commit()` — IntegrityError sul flush (NOT
    NULL), proxy di un errore transitorio di commit a monte (deadlock/timeout/
    connessione caduta su Postgres/Supabase). L'eccezione propaga fino al loop,
    che la cattura come outcome='error'. Dopo `scrape_bios_browser_session` il
    follower NON deve restare stranded: lock rilasciato nonostante la sessione
    avvelenata."""
    base = 990100000000 + int(datetime.utcnow().timestamp()) % 100000
    async with AsyncSessionLocal() as db:
        camp = Campaign(name="advqreg", status=CampaignStatus.scraping, source_type="scrape")
        db.add(camp)
        await db.flush()
        db.add(Follower(campaign_id=camp.id, ig_user_id=base,
                        username=f"u{base}", status=FollowerStatus.pending))
        await db.commit()
        cid = camp.id

    _patch_common(monkeypatch, cap=5)

    async def fake_fetch(follower, campaign, db, session):
        follower.biography = "scritto prima del poisoning"
        follower.username = None  # NOT NULL -> IntegrityError sul commit sottostante
        await db.commit()
        return "done", None  # mai raggiunto: l'eccezione del commit sopra propaga prima
    monkeypatch.setattr(browser_bio, "fetch_and_store_bio_browser", fake_fetch)

    defer = await browser_bio.scrape_bios_browser_session(cid, "acc-A")
    assert defer is None, "pool esaurito (1 follower, skippato) -> None atteso"

    async with AsyncSessionLocal() as db:
        f = (await db.execute(
            select(Follower).where(Follower.campaign_id == cid)
        )).scalar_one()
        assert f.locked_by_account_id is None, (
            "follower STRANDED pending+locked dopo una sessione DB avvelenata")
        assert f.locked_at is None
        assert f.status == FollowerStatus.skipped
        assert f.skip_reason == "browser_error"


@pytest.mark.asyncio
async def test_single_account_exact_cap_drain_completes(monkeypatch):
    """BUG 2: pool con pending == cap esatto. L'account li scrapa tutti, il loop
    esce dal ramo `done_count >= cap` col pool ORA vuoto: deve completare subito
    la campagna (ready) invece di ritornare la pausa lunga anti-block a vuoto."""
    base = 990200000000 + int(datetime.utcnow().timestamp()) % 100000
    CAP = 4
    async with AsyncSessionLocal() as db:
        camp = Campaign(name="advqreg", status=CampaignStatus.scraping, source_type="scrape")
        db.add(camp)
        await db.flush()
        for i in range(CAP):
            db.add(Follower(campaign_id=camp.id, ig_user_id=base + i,
                            username=f"u{base+i}", status=FollowerStatus.pending))
        await db.commit()
        cid = camp.id

    _patch_common(monkeypatch, cap=CAP)

    async def fake_fetch(follower, campaign, db, session):
        follower.status = FollowerStatus.bio_scraped
        follower.locked_by_account_id = None
        follower.locked_at = None
        await db.commit()
        return "done", None
    monkeypatch.setattr(browser_bio, "fetch_and_store_bio_browser", fake_fetch)

    await browser_bio.scrape_bios_browser_session(cid, "acc-SOLO")

    async with AsyncSessionLocal() as db:
        bio_scraped = await db.scalar(
            select(func.count()).select_from(Follower).where(
                Follower.campaign_id == cid,
                Follower.status == FollowerStatus.bio_scraped))
        pending = await db.scalar(
            select(func.count()).select_from(Follower).where(
                Follower.campaign_id == cid,
                Follower.status == FollowerStatus.pending))
        campaign = (await db.execute(
            select(Campaign).where(Campaign.id == cid)
        )).scalar_one()

    assert bio_scraped == CAP
    assert pending == 0
    assert campaign.status == CampaignStatus.ready, (
        f"pool drenato esattamente al cap ma campagna={campaign.status}: "
        "resta 'scraping' con 0 pending per l'intera pausa anti-block")
