"""Mini-sessione browser: rispetta il cap, scrapa i claimati, ritorna il defer."""
from datetime import datetime
import pytest

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.services import browser_bio


class _FakeSession:
    def __init__(self, *a, **k): self.opened = False; self.closed = False
    async def open(self): self.opened = True
    async def close(self): self.closed = True
    class _P:
        async def ensure_logged_in(self, account_id, allow_login=True): return None
    page = _P()


@pytest.mark.asyncio
async def test_session_scrapes_up_to_cap_and_returns_defer(monkeypatch):
    base = 960000000000 + int(datetime.utcnow().timestamp()) % 100000
    async with AsyncSessionLocal() as db:
        camp = Campaign(name="t", status=CampaignStatus.scraping, source_type="scrape")
        db.add(camp); await db.flush()
        for i in range(5):
            db.add(Follower(campaign_id=camp.id, ig_user_id=base + i,
                            username=f"u{base+i}", status=FollowerStatus.pending))
        await db.commit()
        cid = camp.id

    # cap piccolo per forzare il defer prima di esaurire i pending
    monkeypatch.setattr(browser_bio, "BrowserSession", _FakeSession)
    monkeypatch.setattr(browser_bio, "pick_session_cap", lambda *a, **k: 2)

    async def fake_fetch(follower, campaign, db, session):
        follower.status = FollowerStatus.bio_scraped
        follower.locked_by_account_id = None
        follower.locked_at = None
        await db.commit()
        return "done", None
    monkeypatch.setattr(browser_bio, "fetch_and_store_bio_browser", fake_fetch)
    monkeypatch.setattr(browser_bio, "human_profile_pause", lambda: _anoop())
    monkeypatch.setattr(browser_bio, "maybe_micro_scroll", lambda *a, **k: _anoop_false())

    defer = await browser_bio.scrape_bios_browser_session(cid, "acc-A")
    assert isinstance(defer, int) and defer >= 60      # pausa lunga → defer
    async with AsyncSessionLocal() as db:
        from sqlalchemy import select, func
        done = await db.scalar(
            select(func.count()).select_from(Follower).where(
                Follower.campaign_id == cid,
                Follower.status == FollowerStatus.bio_scraped))
        assert done == 2                                # esattamente il cap


async def _anoop(): return None
async def _anoop_false(): return False


@pytest.mark.asyncio
async def test_skip_outcome_releases_lock_and_continues(monkeypatch):
    base = 961000000000 + int(datetime.utcnow().timestamp()) % 100000
    async with AsyncSessionLocal() as db:
        camp = Campaign(name="t", status=CampaignStatus.scraping, source_type="scrape")
        db.add(camp); await db.flush()
        for i in range(5):
            db.add(Follower(campaign_id=camp.id, ig_user_id=base + i,
                            username=f"u{base+i}", status=FollowerStatus.pending))
        await db.commit()
        cid = camp.id

    monkeypatch.setattr(browser_bio, "BrowserSession", _FakeSession)
    monkeypatch.setattr(browser_bio, "pick_session_cap", lambda *a, **k: 1)

    calls = {"n": 0}

    async def fake_fetch(follower, campaign, db, session):
        calls["n"] += 1
        if calls["n"] == 1:
            return "not_found", None
        follower.status = FollowerStatus.bio_scraped
        follower.locked_by_account_id = None
        follower.locked_at = None
        await db.commit()
        return "done", None
    monkeypatch.setattr(browser_bio, "fetch_and_store_bio_browser", fake_fetch)
    monkeypatch.setattr(browser_bio, "human_profile_pause", lambda: _anoop())
    monkeypatch.setattr(browser_bio, "maybe_micro_scroll", lambda *a, **k: _anoop_false())

    defer = await browser_bio.scrape_bios_browser_session(cid, "acc-A")
    assert isinstance(defer, int) and defer >= 60  # cap (1) raggiunto -> pausa lunga

    async with AsyncSessionLocal() as db:
        from sqlalchemy import select
        skipped = (await db.execute(
            select(Follower).where(
                Follower.campaign_id == cid,
                Follower.status == FollowerStatus.skipped,
            )
        )).scalars().all()
        assert len(skipped) == 1
        assert skipped[0].skip_reason == "browser_not_found"
        assert skipped[0].locked_by_account_id is None

        done = (await db.execute(
            select(Follower).where(
                Follower.campaign_id == cid,
                Follower.status == FollowerStatus.bio_scraped,
            )
        )).scalars().all()
        assert len(done) == 1


@pytest.mark.asyncio
async def test_soft_block_backoff_defer_and_releases_lock(monkeypatch):
    base = 962000000000 + int(datetime.utcnow().timestamp()) % 100000
    async with AsyncSessionLocal() as db:
        camp = Campaign(name="t", status=CampaignStatus.scraping, source_type="scrape")
        db.add(camp); await db.flush()
        db.add(Follower(campaign_id=camp.id, ig_user_id=base,
                        username=f"u{base}", status=FollowerStatus.pending))
        await db.commit()
        cid = camp.id

    monkeypatch.setattr(browser_bio, "BrowserSession", _FakeSession)
    monkeypatch.setattr(browser_bio, "pick_session_cap", lambda *a, **k: 5)

    async def fake_fetch(follower, campaign, db, session):
        return "soft_block", Exception("429")
    monkeypatch.setattr(browser_bio, "fetch_and_store_bio_browser", fake_fetch)
    monkeypatch.setattr(browser_bio, "human_profile_pause", lambda: _anoop())
    monkeypatch.setattr(browser_bio, "maybe_micro_scroll", lambda *a, **k: _anoop_false())

    defer = await browser_bio.scrape_bios_browser_session(cid, "acc-A")
    # non piu' None: backoff defer (15-30min) cosi' l'account NON resta sideline,
    # il worker fa Retry(defer=...) e riprova piu' tardi invece di abbandonare la run.
    assert isinstance(defer, int) and 900 <= defer <= 1800

    async with AsyncSessionLocal() as db:
        from sqlalchemy import select
        f = (await db.execute(
            select(Follower).where(Follower.campaign_id == cid)
        )).scalar_one()
        assert f.status == FollowerStatus.pending  # non bruciato
        assert f.locked_by_account_id is None       # lock rilasciato

        # la campagna resta scraping: il backoff non e' un completamento
        c = (await db.execute(
            select(Campaign).where(Campaign.id == cid)
        )).scalar_one()
        assert c.status == CampaignStatus.scraping


@pytest.mark.asyncio
async def test_network_returns_short_defer(monkeypatch):
    base = 964000000000 + int(datetime.utcnow().timestamp()) % 100000
    async with AsyncSessionLocal() as db:
        camp = Campaign(name="t", status=CampaignStatus.scraping, source_type="scrape")
        db.add(camp); await db.flush()
        db.add(Follower(campaign_id=camp.id, ig_user_id=base,
                        username=f"u{base}", status=FollowerStatus.pending))
        await db.commit()
        cid = camp.id

    monkeypatch.setattr(browser_bio, "BrowserSession", _FakeSession)
    monkeypatch.setattr(browser_bio, "pick_session_cap", lambda *a, **k: 5)

    async def fake_fetch(follower, campaign, db, session):
        return "network", Exception("net down")
    monkeypatch.setattr(browser_bio, "fetch_and_store_bio_browser", fake_fetch)
    monkeypatch.setattr(browser_bio, "human_profile_pause", lambda: _anoop())
    monkeypatch.setattr(browser_bio, "maybe_micro_scroll", lambda *a, **k: _anoop_false())

    defer = await browser_bio.scrape_bios_browser_session(cid, "acc-A")
    assert defer == 180  # retry breve (3 min), non sideline silenzioso

    async with AsyncSessionLocal() as db:
        from sqlalchemy import select
        f = (await db.execute(
            select(Follower).where(Follower.campaign_id == cid)
        )).scalar_one()
        assert f.status == FollowerStatus.pending  # non bruciato
        assert f.locked_by_account_id is None       # lock rilasciato


@pytest.mark.asyncio
async def test_pool_drained_completes_campaign(monkeypatch):
    """Pool piccolo, cap abbastanza grande da svuotarlo tutto: la campagna deve
    passare a 'ready' e deve partire l'evento scrape_complete (finding 1)."""
    base = 965000000000 + int(datetime.utcnow().timestamp()) % 100000
    async with AsyncSessionLocal() as db:
        camp = Campaign(name="t", status=CampaignStatus.scraping, source_type="scrape")
        db.add(camp); await db.flush()
        for i in range(3):
            db.add(Follower(campaign_id=camp.id, ig_user_id=base + i,
                            username=f"u{base+i}", status=FollowerStatus.pending))
        await db.commit()
        cid = camp.id

    monkeypatch.setattr(browser_bio, "BrowserSession", _FakeSession)
    monkeypatch.setattr(browser_bio, "pick_session_cap", lambda *a, **k: 10)  # >> pending

    async def fake_fetch(follower, campaign, db, session):
        follower.status = FollowerStatus.bio_scraped
        follower.locked_by_account_id = None
        follower.locked_at = None
        await db.commit()
        return "done", None
    monkeypatch.setattr(browser_bio, "fetch_and_store_bio_browser", fake_fetch)
    monkeypatch.setattr(browser_bio, "human_profile_pause", lambda: _anoop())
    monkeypatch.setattr(browser_bio, "maybe_micro_scroll", lambda *a, **k: _anoop_false())

    # emit e' importato dentro la funzione (`from app.utils.events import emit as
    # emit_event`), quindi il monkeypatch va sul modulo sorgente `app.utils.events`,
    # non su `browser_bio` (li' non esiste un attributo modulo-level da patchare).
    from app.utils import events as events_module
    emitted = []
    monkeypatch.setattr(events_module, "emit", lambda *a, **k: emitted.append((a, k)))

    defer = await browser_bio.scrape_bios_browser_session(cid, "acc-A")
    assert defer is None  # pool esaurito, non e' una pausa-cap

    async with AsyncSessionLocal() as db:
        from sqlalchemy import select
        c = (await db.execute(
            select(Campaign).where(Campaign.id == cid)
        )).scalar_one()
        assert c.status == CampaignStatus.ready  # handoff verso la Fase DM abilitato

    assert any(call[0][1] == "scrape_complete" for call in emitted), (
        "scrape_complete non emesso al drain del pool"
    )


@pytest.mark.asyncio
async def test_pool_exhausted_returns_none(monkeypatch):
    async with AsyncSessionLocal() as db:
        camp = Campaign(name="t", status=CampaignStatus.scraping, source_type="scrape")
        db.add(camp); await db.commit()
        cid = camp.id

    monkeypatch.setattr(browser_bio, "BrowserSession", _FakeSession)
    monkeypatch.setattr(browser_bio, "pick_session_cap", lambda *a, **k: 5)

    async def fake_fetch(follower, campaign, db, session):
        raise AssertionError("non deve essere chiamato: nessun pending")
    monkeypatch.setattr(browser_bio, "fetch_and_store_bio_browser", fake_fetch)
    monkeypatch.setattr(browser_bio, "human_profile_pause", lambda: _anoop())
    monkeypatch.setattr(browser_bio, "maybe_micro_scroll", lambda *a, **k: _anoop_false())

    defer = await browser_bio.scrape_bios_browser_session(cid, "acc-A")
    assert defer is None

    # Con 0 pending e nessun bio_target, _maybe_complete_browser_bio (finding 1) ORA
    # porta la campagna a 'ready' invece di lasciarla bloccata su 'scraping' per
    # sempre: e' il comportamento corretto (handoff verso la Fase DM), non piu' un
    # side-effect da evitare.
    async with AsyncSessionLocal() as db:
        from sqlalchemy import select
        c = (await db.execute(
            select(Campaign).where(Campaign.id == cid)
        )).scalar_one()
        assert c.status == CampaignStatus.ready


@pytest.mark.asyncio
async def test_skip_heavy_pool_hits_backstop_short_defer(monkeypatch):
    base = 963000000000 + int(datetime.utcnow().timestamp()) % 100000
    cap = 2
    max_iterations = cap * browser_bio.MAX_SESSION_ITERATIONS_MULTIPLIER
    # pool abbondante: molto piu' grande del backstop, cosi' non si esaurisce prima
    n_followers = max_iterations + 10
    async with AsyncSessionLocal() as db:
        camp = Campaign(name="t", status=CampaignStatus.scraping, source_type="scrape")
        db.add(camp); await db.flush()
        for i in range(n_followers):
            db.add(Follower(campaign_id=camp.id, ig_user_id=base + i,
                            username=f"u{base+i}", status=FollowerStatus.pending))
        await db.commit()
        cid = camp.id

    monkeypatch.setattr(browser_bio, "BrowserSession", _FakeSession)
    monkeypatch.setattr(browser_bio, "pick_session_cap", lambda *a, **k: cap)

    calls = {"n": 0}

    async def fake_fetch(follower, campaign, db, session):
        calls["n"] += 1
        return "private", None  # mai 'done': il pool e' tutto skip-heavy
    monkeypatch.setattr(browser_bio, "fetch_and_store_bio_browser", fake_fetch)
    monkeypatch.setattr(browser_bio, "human_profile_pause", lambda: _anoop())
    monkeypatch.setattr(browser_bio, "maybe_micro_scroll", lambda *a, **k: _anoop_false())

    defer = await browser_bio.scrape_bios_browser_session(cid, "acc-A")
    assert defer == 60  # backstop -> defer breve, non la pausa lunga (30-45min)
    assert calls["n"] <= max_iterations


class _FakeReelsPage:
    """Page fake che conta le pause attive sui reel invece di aprirle davvero."""
    def __init__(self, raise_on_call=False):
        self.calls = 0
        self.raise_on_call = raise_on_call

    async def ensure_logged_in(self, account_id, allow_login=True):
        return None

    async def browse_reels(self, *args, **kwargs):
        self.calls += 1
        if self.raise_on_call:
            raise RuntimeError("reels browse boom")


class _FakeSessionWithReels:
    def __init__(self, page): self.opened = False; self.closed = False; self.page = page
    async def open(self): self.opened = True
    async def close(self): self.closed = True


@pytest.mark.asyncio
async def test_reels_break_every_n_profiles(monkeypatch):
    """Cadenza pinnata a 2: ogni 2 profili processati (non solo 'done'), invece
    della pausa stazionaria, deve scattare una pausa ATTIVA sui reel."""
    base = 966000000000 + int(datetime.utcnow().timestamp()) % 100000
    n_followers = 6
    async with AsyncSessionLocal() as db:
        camp = Campaign(name="t", status=CampaignStatus.scraping, source_type="scrape")
        db.add(camp); await db.flush()
        for i in range(n_followers):
            db.add(Follower(campaign_id=camp.id, ig_user_id=base + i,
                            username=f"u{base+i}", status=FollowerStatus.pending))
        await db.commit()
        cid = camp.id

    fake_page = _FakeReelsPage()

    monkeypatch.setattr(browser_bio, "BrowserSession", lambda *a, **k: _FakeSessionWithReels(fake_page))
    monkeypatch.setattr(browser_bio, "pick_session_cap", lambda *a, **k: n_followers)
    monkeypatch.setattr(browser_bio.settings, "bio_browser_reels_every_min", 2)
    monkeypatch.setattr(browser_bio.settings, "bio_browser_reels_every_max", 2)
    monkeypatch.setattr(browser_bio, "human_profile_pause", lambda: _anoop())
    monkeypatch.setattr(browser_bio, "maybe_micro_scroll", lambda *a, **k: _anoop_false())

    async def fake_fetch(follower, campaign, db, session):
        follower.status = FollowerStatus.bio_scraped
        follower.locked_by_account_id = None
        follower.locked_at = None
        follower.is_private = False
        await db.commit()
        return "done", None
    monkeypatch.setattr(browser_bio, "fetch_and_store_bio_browser", fake_fetch)

    await browser_bio.scrape_bios_browser_session(cid, "acc-A")

    # 6 profili, cadenza fissa a 2 -> pausa reel dopo il 2°, 4° e 6° profilo = 3 volte
    assert fake_page.calls == 3


@pytest.mark.asyncio
async def test_reels_break_exception_does_not_break_session(monkeypatch):
    """Un'eccezione dentro browse_reels va ingoiata: la mini-sessione deve
    continuare a processare i profili successivi normalmente."""
    base = 967000000000 + int(datetime.utcnow().timestamp()) % 100000
    n_followers = 4
    async with AsyncSessionLocal() as db:
        camp = Campaign(name="t", status=CampaignStatus.scraping, source_type="scrape")
        db.add(camp); await db.flush()
        for i in range(n_followers):
            db.add(Follower(campaign_id=camp.id, ig_user_id=base + i,
                            username=f"u{base+i}", status=FollowerStatus.pending))
        await db.commit()
        cid = camp.id

    fake_page = _FakeReelsPage(raise_on_call=True)

    monkeypatch.setattr(browser_bio, "BrowserSession", lambda *a, **k: _FakeSessionWithReels(fake_page))
    monkeypatch.setattr(browser_bio, "pick_session_cap", lambda *a, **k: n_followers)
    monkeypatch.setattr(browser_bio.settings, "bio_browser_reels_every_min", 2)
    monkeypatch.setattr(browser_bio.settings, "bio_browser_reels_every_max", 2)
    monkeypatch.setattr(browser_bio, "human_profile_pause", lambda: _anoop())
    monkeypatch.setattr(browser_bio, "maybe_micro_scroll", lambda *a, **k: _anoop_false())

    async def fake_fetch(follower, campaign, db, session):
        follower.status = FollowerStatus.bio_scraped
        follower.locked_by_account_id = None
        follower.locked_at = None
        follower.is_private = False
        await db.commit()
        return "done", None
    monkeypatch.setattr(browser_bio, "fetch_and_store_bio_browser", fake_fetch)

    # non deve sollevare, nonostante browse_reels fallisca sempre
    await browser_bio.scrape_bios_browser_session(cid, "acc-A")

    assert fake_page.calls == 2  # pausa reel provata dopo il 2° e il 4° profilo

    async with AsyncSessionLocal() as db:
        from sqlalchemy import select, func
        done = await db.scalar(
            select(func.count()).select_from(Follower).where(
                Follower.campaign_id == cid,
                Follower.status == FollowerStatus.bio_scraped))
        assert done == n_followers  # tutti i profili sono comunque stati processati


@pytest.mark.asyncio
async def test_no_reels_break_on_soft_block(monkeypatch):
    """Su soft_block la sessione si ferma subito (backoff defer): NESSUNA pausa
    reel deve scattare sul path di stop (cadenza pinnata a 1 per smascherarlo)."""
    base = 955000000000 + int(datetime.utcnow().timestamp()) % 100000
    async with AsyncSessionLocal() as db:
        camp = Campaign(name="t", status=CampaignStatus.scraping, source_type="scrape")
        db.add(camp); await db.flush()
        for i in range(4):
            db.add(Follower(campaign_id=camp.id, ig_user_id=base + i,
                            username=f"u{base+i}", status=FollowerStatus.pending))
        await db.commit()
        cid = camp.id

    fake_page = _FakeReelsPage()
    monkeypatch.setattr(browser_bio, "BrowserSession", lambda *a, **k: _FakeSessionWithReels(fake_page))
    monkeypatch.setattr(browser_bio, "pick_session_cap", lambda *a, **k: 4)
    monkeypatch.setattr(browser_bio.settings, "bio_browser_reels_every_min", 1)
    monkeypatch.setattr(browser_bio.settings, "bio_browser_reels_every_max", 1)
    monkeypatch.setattr(browser_bio, "human_profile_pause", lambda: _anoop())
    monkeypatch.setattr(browser_bio, "maybe_micro_scroll", lambda *a, **k: _anoop_false())

    async def fake_fetch(follower, campaign, db, session):
        return "soft_block", Exception("429")
    monkeypatch.setattr(browser_bio, "fetch_and_store_bio_browser", fake_fetch)

    defer = await browser_bio.scrape_bios_browser_session(cid, "acc-A")
    assert isinstance(defer, int) and defer >= 900   # backoff, non pausa reel
    assert fake_page.calls == 0                       # niente reel sul path di stop


async def _anoop2(*a, **k): return None
async def _anoop2_false(*a, **k): return False


@pytest.mark.asyncio
async def test_soft_block_threshold_pauses_campaign(monkeypatch):
    """Fix C: al N-esimo soft-block consecutivo (contatore pinnato al threshold),
    la campagna va in PAUSA e il defer e' None (stop al 429->defer->429), non un retry."""
    base = 970000000000 + int(datetime.utcnow().timestamp()) % 100000
    async with AsyncSessionLocal() as db:
        camp = Campaign(name="t", status=CampaignStatus.scraping, source_type="scrape")
        db.add(camp); await db.flush()
        for i in range(3):
            db.add(Follower(campaign_id=camp.id, ig_user_id=base + i,
                            username=f"u{base+i}", status=FollowerStatus.pending))
        await db.commit()
        cid = camp.id

    monkeypatch.setattr(browser_bio, "BrowserSession", _FakeSession)
    monkeypatch.setattr(browser_bio, "pick_session_cap", lambda *a, **k: 5)
    monkeypatch.setattr(browser_bio, "human_profile_pause", _anoop2)
    monkeypatch.setattr(browser_bio, "maybe_micro_scroll", _anoop2_false)

    async def fake_fetch(follower, campaign, db, session):
        return "soft_block", Exception("429")
    monkeypatch.setattr(browser_bio, "fetch_and_store_bio_browser", fake_fetch)

    thr = browser_bio.settings.bio_browser_soft_block_pause_threshold

    async def fake_incr(campaign_id, account_id):
        return thr  # simula il N-esimo soft-block consecutivo
    monkeypatch.setattr(browser_bio, "_soft_block_incr", fake_incr)

    defer = await browser_bio.scrape_bios_browser_session(cid, "acc-A")
    assert defer is None  # pausa campagna, non altro retry
    async with AsyncSessionLocal() as db:
        c = await db.get(Campaign, cid)
        assert c.status == CampaignStatus.paused


@pytest.mark.asyncio
async def test_session_expired_isolates_account_and_pauses(monkeypatch):
    """Fix A: sessione scaduta (allow_login=False -> AccountSessionExpiredError) NON
    ritenta all'infinito: isola l'account (challenge_required) e pausa la campagna, defer None."""
    import uuid
    from app.models.account import InstagramAccount, AccountStatus
    from app.utils.exceptions import AccountSessionExpiredError
    base = 971000000000 + int(datetime.utcnow().timestamp()) % 100000
    acc_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        db.add(InstagramAccount(id=acc_id, username=f"acc_exp_{base}",
                                encrypted_password="x", status=AccountStatus.active,
                                daily_message_limit=20))
        camp = Campaign(name="t", status=CampaignStatus.scraping, source_type="scrape")
        db.add(camp); await db.flush()
        db.add(Follower(campaign_id=camp.id, ig_user_id=base,
                        username=f"u{base}", status=FollowerStatus.pending))
        await db.commit()
        cid = camp.id

    class _ExpiredSession:
        def __init__(self, *a, **k): pass
        async def open(self): return None
        async def close(self): return None
        class _P:
            async def ensure_logged_in(self, account_id, allow_login=True):
                raise AccountSessionExpiredError(account_id)
        page = _P()

    monkeypatch.setattr(browser_bio, "BrowserSession", _ExpiredSession)

    defer = await browser_bio.scrape_bios_browser_session(cid, acc_id)
    assert defer is None  # isolato, non retry
    async with AsyncSessionLocal() as db:
        from app.models.account import InstagramAccount as IA, AccountStatus as AS
        c = await db.get(Campaign, cid)
        a = await db.get(IA, acc_id)
        assert c.status == CampaignStatus.paused
        assert a.status == AS.challenge_required
