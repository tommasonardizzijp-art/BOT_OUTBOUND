"""Resolver import via BROWSER (bio_engine='browser' su source_type='import').

Copre: (1) il dispatch runtime in resolve_imports, (2) resolve_and_store_bio_browser
(crea Follower da username, no API), (3) la mini-sessione resolve_imports_browser
(cap/defer, completamento, no-account, soft-block), (4) il task che onora il defer.
"""
from datetime import datetime

import pytest
from sqlalchemy import func, select

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.models.imported_profile import ImportedProfile
from app.services import browser_import as bi


# --------------------------------------------------------------------------- #
# Fakes / helpers
# --------------------------------------------------------------------------- #
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

        async def _get_page(self):
            return object()

        async def browse_reels(self, *a, **k):
            return None

    page = _P()


async def _anoop(*a, **k):
    return None


async def _anoop_false(*a, **k):
    return False


async def _fake_accounts(_campaign_id):
    return [("acc-A", "scraper1")]


def _web_user(pk, username, *, private=False, bio="ciao bio", email=None):
    """Un dict `data.user` di web_profile_info come lo consuma web_user_to_shim."""
    return {
        "id": str(pk),
        "username": username,
        "full_name": "Full Name",
        "biography": bio,
        "is_verified": False,
        "is_private": private,
        "edge_followed_by": {"count": 1200},
        "edge_follow": {"count": 300},
        "external_url": "https://example.com",
        "business_email": email,
        "business_phone_number": None,
        "bio_links": [],
    }


async def _mk_import_campaign(engine="browser", n_pending=0, messaging=True):
    async with AsyncSessionLocal() as db:
        camp = Campaign(
            name="t", status=CampaignStatus.scraping,
            source_type="import", bio_engine=engine, messaging_enabled=messaging,
        )
        db.add(camp)
        await db.flush()
        for i in range(n_pending):
            db.add(ImportedProfile(
                campaign_id=camp.id, raw_input=f"u{i}", username=f"u{i}", status="pending",
            ))
        await db.commit()
        return camp.id


# --------------------------------------------------------------------------- #
# resolve_and_store_bio_browser — crea il Follower da username (no API)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_resolve_and_store_creates_follower_public(monkeypatch):
    cid = await _mk_import_campaign()
    async with AsyncSessionLocal() as db:
        row = ImportedProfile(campaign_id=cid, raw_input="tgt", username="tgt", status="pending")
        db.add(row)
        await db.commit()
        rid = row.id

    monkeypatch.setattr(bi.settings, "bio_browser_contact_info_enabled", False)

    async def fake_capture(raw_page, username, timeout_s=8.0):
        return _web_user(700100001, username, private=False)
    monkeypatch.setattr(bi, "_capture_web_profile_info", fake_capture)

    async with AsyncSessionLocal() as db:
        camp = await db.get(Campaign, cid)
        row = await db.get(ImportedProfile, rid)
        outcome, err = await bi.resolve_and_store_bio_browser(row, camp, db, _FakeSession())
    assert outcome == "done" and err is None

    async with AsyncSessionLocal() as db:
        r = await db.get(ImportedProfile, rid)
        assert r.status == "resolved" and r.ig_user_id == 700100001
        f = (await db.execute(select(Follower).where(Follower.campaign_id == cid))).scalar_one()
        assert f.ig_user_id == 700100001
        assert f.status == FollowerStatus.bio_scraped
        assert f.username == "tgt"
        assert (f.biography or "").startswith("ciao")
        assert f.follower_count == 1200


@pytest.mark.asyncio
async def test_resolve_and_store_private_still_creates(monkeypatch):
    cid = await _mk_import_campaign()
    async with AsyncSessionLocal() as db:
        row = ImportedProfile(campaign_id=cid, raw_input="priv", username="priv", status="pending")
        db.add(row)
        await db.commit()
        rid = row.id

    monkeypatch.setattr(bi.settings, "bio_browser_contact_info_enabled", False)

    async def fake_capture(raw_page, username, timeout_s=8.0):
        return _web_user(700100002, username, private=True)
    monkeypatch.setattr(bi, "_capture_web_profile_info", fake_capture)

    async with AsyncSessionLocal() as db:
        camp = await db.get(Campaign, cid)
        row = await db.get(ImportedProfile, rid)
        outcome, _ = await bi.resolve_and_store_bio_browser(row, camp, db, _FakeSession())
    assert outcome == "done"

    async with AsyncSessionLocal() as db:
        r = await db.get(ImportedProfile, rid)
        assert r.status == "private"
        f = (await db.execute(select(Follower).where(Follower.campaign_id == cid))).scalar_one()
        assert f.is_private is True


@pytest.mark.asyncio
async def test_resolve_and_store_not_found(monkeypatch):
    cid = await _mk_import_campaign()
    async with AsyncSessionLocal() as db:
        row = ImportedProfile(campaign_id=cid, raw_input="ghost", username="ghost", status="pending")
        db.add(row)
        await db.commit()
        rid = row.id

    async def fake_capture(raw_page, username, timeout_s=8.0):
        return None  # profilo inesistente
    monkeypatch.setattr(bi, "_capture_web_profile_info", fake_capture)

    async with AsyncSessionLocal() as db:
        camp = await db.get(Campaign, cid)
        row = await db.get(ImportedProfile, rid)
        outcome, _ = await bi.resolve_and_store_bio_browser(row, camp, db, _FakeSession())
    assert outcome == "not_found"

    async with AsyncSessionLocal() as db:
        r = await db.get(ImportedProfile, rid)
        assert r.status == "not_found"
        n = await db.scalar(select(func.count()).select_from(Follower).where(Follower.campaign_id == cid))
        assert n == 0


@pytest.mark.asyncio
async def test_resolve_and_store_soft_block_keeps_pending(monkeypatch):
    cid = await _mk_import_campaign()
    async with AsyncSessionLocal() as db:
        row = ImportedProfile(campaign_id=cid, raw_input="rl", username="rl", status="pending")
        db.add(row)
        await db.commit()
        rid = row.id

    async def fake_capture(raw_page, username, timeout_s=8.0):
        return {"__status": 429}
    monkeypatch.setattr(bi, "_capture_web_profile_info", fake_capture)

    async with AsyncSessionLocal() as db:
        camp = await db.get(Campaign, cid)
        row = await db.get(ImportedProfile, rid)
        outcome, _ = await bi.resolve_and_store_bio_browser(row, camp, db, _FakeSession())
    assert outcome == "soft_block"

    async with AsyncSessionLocal() as db:
        r = await db.get(ImportedProfile, rid)
        assert r.status == "pending"  # NON bruciato: verra' ritentato
        n = await db.scalar(select(func.count()).select_from(Follower).where(Follower.campaign_id == cid))
        assert n == 0


@pytest.mark.asyncio
async def test_resolve_and_store_dedup_no_duplicate(monkeypatch):
    cid = await _mk_import_campaign()
    async with AsyncSessionLocal() as db:
        # Follower gia' presente con lo stesso pk (username duplicato / gia' risolto)
        db.add(Follower(campaign_id=cid, ig_user_id=700100003, username="dup",
                        status=FollowerStatus.bio_scraped))
        row = ImportedProfile(campaign_id=cid, raw_input="dup", username="dup", status="pending")
        db.add(row)
        await db.commit()
        rid = row.id

    monkeypatch.setattr(bi.settings, "bio_browser_contact_info_enabled", False)

    async def fake_capture(raw_page, username, timeout_s=8.0):
        return _web_user(700100003, username)
    monkeypatch.setattr(bi, "_capture_web_profile_info", fake_capture)

    async with AsyncSessionLocal() as db:
        camp = await db.get(Campaign, cid)
        row = await db.get(ImportedProfile, rid)
        outcome, _ = await bi.resolve_and_store_bio_browser(row, camp, db, _FakeSession())
    assert outcome == "done"

    async with AsyncSessionLocal() as db:
        r = await db.get(ImportedProfile, rid)
        assert r.status == "resolved"
        n = await db.scalar(select(func.count()).select_from(Follower).where(Follower.campaign_id == cid))
        assert n == 1  # nessun duplicato


# --------------------------------------------------------------------------- #
# resolve_imports_browser — mini-sessione
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_session_resolves_up_to_cap_and_defers(monkeypatch):
    cid = await _mk_import_campaign(n_pending=5)

    monkeypatch.setattr(bi, "BrowserSession", _FakeSession)
    monkeypatch.setattr(bi, "pick_session_cap", lambda *a, **k: 2)
    monkeypatch.setattr(bi, "_scraping_accounts_of_campaign", _fake_accounts)
    monkeypatch.setattr(bi, "human_profile_pause", _anoop)
    monkeypatch.setattr(bi, "maybe_micro_scroll", _anoop_false)

    async def fake_resolve(row, campaign, db, session):
        row.status = "resolved"
        row.ig_user_id = 700200000 + int(row.username[1:])
        row.updated_at = datetime.utcnow()
        await db.commit()
        return "done", None
    monkeypatch.setattr(bi, "resolve_and_store_bio_browser", fake_resolve)

    defer = await bi.resolve_imports_browser(cid)
    assert isinstance(defer, int) and defer >= 60  # cap raggiunto -> pausa lunga

    async with AsyncSessionLocal() as db:
        resolved = await db.scalar(
            select(func.count()).select_from(ImportedProfile).where(
                ImportedProfile.campaign_id == cid, ImportedProfile.status == "resolved"))
        assert resolved == 2  # esattamente il cap


@pytest.mark.asyncio
async def test_session_drains_pool_and_completes(monkeypatch):
    cid = await _mk_import_campaign(n_pending=3, messaging=True)

    monkeypatch.setattr(bi, "BrowserSession", _FakeSession)
    monkeypatch.setattr(bi, "pick_session_cap", lambda *a, **k: 10)  # >> pending
    monkeypatch.setattr(bi, "_scraping_accounts_of_campaign", _fake_accounts)
    monkeypatch.setattr(bi, "human_profile_pause", _anoop)
    monkeypatch.setattr(bi, "maybe_micro_scroll", _anoop_false)

    async def fake_resolve(row, campaign, db, session):
        row.status = "resolved"
        row.ig_user_id = 700300000 + int(row.username[1:])
        row.updated_at = datetime.utcnow()
        db.add(Follower(campaign_id=cid, ig_user_id=row.ig_user_id,
                        username=row.username, status=FollowerStatus.bio_scraped))
        await db.commit()
        return "done", None
    monkeypatch.setattr(bi, "resolve_and_store_bio_browser", fake_resolve)

    defer = await bi.resolve_imports_browser(cid)
    assert defer is None  # pool esaurito -> completa, non pausa

    async with AsyncSessionLocal() as db:
        c = await db.get(Campaign, cid)
        assert c.status == CampaignStatus.ready
        assert c.total_followers == 3
        assert c.messages_pending == 3
        assert c.scrape_outcome == "completed"


@pytest.mark.asyncio
async def test_session_completes_when_no_pending(monkeypatch):
    cid = await _mk_import_campaign(n_pending=0, messaging=False)

    # Non deve nemmeno aprire il browser: 0 pending -> completa subito.
    def _boom(*a, **k):
        raise AssertionError("BrowserSession non deve aprirsi senza pending")
    monkeypatch.setattr(bi, "BrowserSession", _boom)

    defer = await bi.resolve_imports_browser(cid)
    assert defer is None
    async with AsyncSessionLocal() as db:
        c = await db.get(Campaign, cid)
        # messaging_enabled=False -> completed (non ready)
        assert c.status == CampaignStatus.completed


@pytest.mark.asyncio
async def test_session_no_active_account_sets_error(monkeypatch):
    # 1 pending, ma NESSUN account scraping attivo (query reale -> lista vuota).
    cid = await _mk_import_campaign(n_pending=1)

    def _boom(*a, **k):
        raise AssertionError("BrowserSession non deve aprirsi senza account")
    monkeypatch.setattr(bi, "BrowserSession", _boom)

    defer = await bi.resolve_imports_browser(cid)
    assert defer is None
    async with AsyncSessionLocal() as db:
        c = await db.get(Campaign, cid)
        assert c.status == CampaignStatus.error
        assert c.scrape_outcome == "scrape_no_account"


@pytest.mark.asyncio
async def test_session_soft_block_threshold_pauses(monkeypatch):
    cid = await _mk_import_campaign(n_pending=3)

    monkeypatch.setattr(bi, "BrowserSession", _FakeSession)
    monkeypatch.setattr(bi, "pick_session_cap", lambda *a, **k: 5)
    monkeypatch.setattr(bi, "_scraping_accounts_of_campaign", _fake_accounts)
    monkeypatch.setattr(bi, "human_profile_pause", _anoop)
    monkeypatch.setattr(bi, "maybe_micro_scroll", _anoop_false)

    async def fake_resolve(row, campaign, db, session):
        return "soft_block", Exception("429")
    monkeypatch.setattr(bi, "resolve_and_store_bio_browser", fake_resolve)

    thr = bi.settings.bio_browser_soft_block_pause_threshold

    async def fake_incr(campaign_id, account_id):
        return thr  # N-esimo soft-block consecutivo
    monkeypatch.setattr(bi, "_soft_block_incr", fake_incr)

    defer = await bi.resolve_imports_browser(cid)
    assert defer is None  # pausa campagna, non retry
    async with AsyncSessionLocal() as db:
        c = await db.get(Campaign, cid)
        assert c.status == CampaignStatus.paused
        # profili NON bruciati: restano pending
        pend = await db.scalar(
            select(func.count()).select_from(ImportedProfile).where(
                ImportedProfile.campaign_id == cid, ImportedProfile.status == "pending"))
        assert pend == 3


@pytest.mark.asyncio
async def test_session_soft_block_below_threshold_backoff(monkeypatch):
    cid = await _mk_import_campaign(n_pending=3)

    monkeypatch.setattr(bi, "BrowserSession", _FakeSession)
    monkeypatch.setattr(bi, "pick_session_cap", lambda *a, **k: 5)
    monkeypatch.setattr(bi, "_scraping_accounts_of_campaign", _fake_accounts)
    monkeypatch.setattr(bi, "human_profile_pause", _anoop)
    monkeypatch.setattr(bi, "maybe_micro_scroll", _anoop_false)

    async def fake_resolve(row, campaign, db, session):
        return "soft_block", Exception("429")
    monkeypatch.setattr(bi, "resolve_and_store_bio_browser", fake_resolve)

    async def fake_incr(campaign_id, account_id):
        return 1  # prima occorrenza
    monkeypatch.setattr(bi, "_soft_block_incr", fake_incr)

    defer = await bi.resolve_imports_browser(cid)
    assert isinstance(defer, int) and 900 <= defer <= 1800  # backoff, non pausa
    async with AsyncSessionLocal() as db:
        c = await db.get(Campaign, cid)
        assert c.status == CampaignStatus.scraping  # NON pausata


# --------------------------------------------------------------------------- #
# Dispatch runtime in resolve_imports (import_resolver)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_resolve_imports_dispatches_to_browser(monkeypatch):
    from app.services import import_resolver as ir
    cid = await _mk_import_campaign(engine="browser", n_pending=1)

    called = {}

    async def fake_browser(campaign_id):
        called["cid"] = campaign_id
        return 321
    monkeypatch.setattr(bi, "resolve_imports_browser", fake_browser)

    async def fake_build(*a, **k):
        raise AssertionError("il path API non deve girare per bio_engine=browser")
    monkeypatch.setattr(ir.ScrapingPool, "build", staticmethod(fake_build))

    result = await ir.resolve_imports(cid)
    assert result == 321
    assert called["cid"] == cid


@pytest.mark.asyncio
async def test_resolve_imports_api_path_when_engine_api(monkeypatch):
    from app.services import import_resolver as ir
    from app.services.scraping_pool import ScrapingPoolEmpty
    cid = await _mk_import_campaign(engine="api", n_pending=1)

    async def fake_browser(campaign_id):
        raise AssertionError("il path browser non deve girare per bio_engine=api")
    monkeypatch.setattr(bi, "resolve_imports_browser", fake_browser)

    async def fake_build(*a, **k):
        raise ScrapingPoolEmpty("no accounts (test)")
    monkeypatch.setattr(ir.ScrapingPool, "build", staticmethod(fake_build))

    result = await ir.resolve_imports(cid)
    assert result is None
    async with AsyncSessionLocal() as db:
        c = await db.get(Campaign, cid)
        assert c.status == CampaignStatus.error  # API path -> pool build -> ScrapingPoolEmpty


# --------------------------------------------------------------------------- #
# Task ARQ onora il defer del motore browser
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_task_raises_retry_on_browser_defer(monkeypatch):
    from app.workers import import_worker as iw
    from arq.worker import Retry

    async def fake_resolve(campaign_id):
        return 300
    monkeypatch.setattr(iw, "resolve_imports", fake_resolve)

    with pytest.raises(Retry):
        await iw.resolve_imports_task({}, "cid-x")


@pytest.mark.asyncio
async def test_task_no_retry_when_none(monkeypatch):
    from app.workers import import_worker as iw

    async def fake_resolve(campaign_id):
        return None
    monkeypatch.setattr(iw, "resolve_imports", fake_resolve)

    # non deve sollevare
    await iw.resolve_imports_task({}, "cid-x")


# --------------------------------------------------------------------------- #
# Guardia single-job all'enqueue (invariante di correttezza del path browser:
# ImportedProfile non ha row-lock, quindi DEVE girare UN solo resolve per campagna)
# --------------------------------------------------------------------------- #
class _FakeRedis:
    def __init__(self, in_progress=False):
        self._inprog = in_progress
        self.deleted = []
        self.enqueued = []

    async def exists(self, key):
        return 1 if (self._inprog and "in-progress" in key) else 0

    async def delete(self, *keys):
        self.deleted.extend(keys)

    async def enqueue_job(self, *a, **k):
        self.enqueued.append((a, k))


@pytest.mark.asyncio
async def test_enqueue_resolve_skips_when_in_progress():
    from app.services.work_enqueue import _enqueue_resolve_with_redis
    r = _FakeRedis(in_progress=True)
    ok = await _enqueue_resolve_with_redis(r, "cid1")
    assert ok is False               # no-op: job gia' in corso
    assert r.enqueued == []          # NESSUN secondo resolve concorrente
    assert not any("in-progress" in k for k in r.deleted)  # lock in-progress preservato


@pytest.mark.asyncio
async def test_enqueue_resolve_runs_when_free():
    from app.services.work_enqueue import _enqueue_resolve_with_redis
    r = _FakeRedis(in_progress=False)
    ok = await _enqueue_resolve_with_redis(r, "cid1")
    assert ok is True
    assert len(r.enqueued) == 1
    assert not any("in-progress" in k for k in r.deleted)  # non cancella MAI l'in-progress


# --------------------------------------------------------------------------- #
# Path di stop/recovery della mini-sessione
# --------------------------------------------------------------------------- #
class _FatalLoginSession:
    def __init__(self, *a, **k):
        pass

    async def open(self):
        return None

    async def close(self):
        return None

    class _P:
        async def ensure_logged_in(self, account_id, allow_login=True):
            from app.utils.exceptions import AccountChallengeError
            raise AccountChallengeError(account_id)

    page = _P()


@pytest.mark.asyncio
async def test_session_fatal_account_isolates_and_pauses(monkeypatch):
    cid = await _mk_import_campaign(n_pending=2)
    monkeypatch.setattr(bi, "BrowserSession", _FatalLoginSession)
    monkeypatch.setattr(bi, "_scraping_accounts_of_campaign", _fake_accounts)

    defer = await bi.resolve_imports_browser(cid)
    assert defer is None  # account isolato, NON un retry
    async with AsyncSessionLocal() as db:
        c = await db.get(Campaign, cid)
        assert c.status == CampaignStatus.paused


@pytest.mark.asyncio
async def test_session_resolve_exception_marks_error_and_advances(monkeypatch):
    cid = await _mk_import_campaign(n_pending=2)
    monkeypatch.setattr(bi, "BrowserSession", _FakeSession)
    monkeypatch.setattr(bi, "pick_session_cap", lambda *a, **k: 5)
    monkeypatch.setattr(bi, "_scraping_accounts_of_campaign", _fake_accounts)
    monkeypatch.setattr(bi, "human_profile_pause", _anoop)
    monkeypatch.setattr(bi, "maybe_micro_scroll", _anoop_false)

    async def boom(row, campaign, db, session):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(bi, "resolve_and_store_bio_browser", boom)

    defer = await bi.resolve_imports_browser(cid)
    assert defer is None  # tutte error -> pool esaurito -> completa
    async with AsyncSessionLocal() as db:
        errs = await db.scalar(
            select(func.count()).select_from(ImportedProfile).where(
                ImportedProfile.campaign_id == cid, ImportedProfile.status == "error"))
        assert errs == 2  # marcate error (NON stuck pending): il loop avanza
        c = await db.get(Campaign, cid)
        assert c.status == CampaignStatus.ready


@pytest.mark.asyncio
async def test_session_network_defers_180(monkeypatch):
    cid = await _mk_import_campaign(n_pending=2)
    monkeypatch.setattr(bi, "BrowserSession", _FakeSession)
    monkeypatch.setattr(bi, "pick_session_cap", lambda *a, **k: 5)
    monkeypatch.setattr(bi, "_scraping_accounts_of_campaign", _fake_accounts)
    monkeypatch.setattr(bi, "human_profile_pause", _anoop)
    monkeypatch.setattr(bi, "maybe_micro_scroll", _anoop_false)

    async def net(row, campaign, db, session):
        return "network", Exception("net:: down")
    monkeypatch.setattr(bi, "resolve_and_store_bio_browser", net)

    defer = await bi.resolve_imports_browser(cid)
    assert defer == 180  # retry breve
    async with AsyncSessionLocal() as db:
        pend = await db.scalar(
            select(func.count()).select_from(ImportedProfile).where(
                ImportedProfile.campaign_id == cid, ImportedProfile.status == "pending"))
        assert pend == 2  # nessuna riga bruciata


@pytest.mark.asyncio
async def test_session_backstop_short_defer(monkeypatch):
    cap = 2
    max_it = cap * bi.MAX_SESSION_ITERATIONS_MULTIPLIER
    cid = await _mk_import_campaign(n_pending=max_it + 5)
    monkeypatch.setattr(bi, "BrowserSession", _FakeSession)
    monkeypatch.setattr(bi, "pick_session_cap", lambda *a, **k: cap)
    monkeypatch.setattr(bi, "_scraping_accounts_of_campaign", _fake_accounts)
    monkeypatch.setattr(bi, "human_profile_pause", _anoop)
    monkeypatch.setattr(bi, "maybe_micro_scroll", _anoop_false)

    calls = {"n": 0}

    async def nf(row, campaign, db, session):
        calls["n"] += 1
        return "not_found", None  # non marca la row -> pool mai drenato
    monkeypatch.setattr(bi, "resolve_and_store_bio_browser", nf)

    defer = await bi.resolve_imports_browser(cid)
    assert defer == 60             # backstop -> defer breve (non la pausa 30-45 min)
    assert calls["n"] <= max_it    # non gira all'infinito


@pytest.mark.asyncio
async def test_resolve_and_store_no_pk_error(monkeypatch):
    cid = await _mk_import_campaign()
    async with AsyncSessionLocal() as db:
        row = ImportedProfile(campaign_id=cid, raw_input="nopk", username="nopk", status="pending")
        db.add(row)
        await db.commit()
        rid = row.id

    monkeypatch.setattr(bi.settings, "bio_browser_contact_info_enabled", False)

    async def fake_capture(raw_page, username, timeout_s=8.0):
        return {"username": username, "biography": "x"}  # dict SENZA 'id'
    monkeypatch.setattr(bi, "_capture_web_profile_info", fake_capture)

    async with AsyncSessionLocal() as db:
        camp = await db.get(Campaign, cid)
        row = await db.get(ImportedProfile, rid)
        outcome, _ = await bi.resolve_and_store_bio_browser(row, camp, db, _FakeSession())
    assert outcome == "error"

    async with AsyncSessionLocal() as db:
        r = await db.get(ImportedProfile, rid)
        assert r.status == "error" and r.error == "no_pk"
        n = await db.scalar(select(func.count()).select_from(Follower).where(Follower.campaign_id == cid))
        assert n == 0


@pytest.mark.asyncio
async def test_resolve_and_store_http_error_non_rate(monkeypatch):
    cid = await _mk_import_campaign()
    async with AsyncSessionLocal() as db:
        row = ImportedProfile(campaign_id=cid, raw_input="g", username="g", status="pending")
        db.add(row)
        await db.commit()
        rid = row.id

    async def fake_capture(raw_page, username, timeout_s=8.0):
        return {"__status": 404}  # non 429/401/403 -> errore terminale, non soft_block
    monkeypatch.setattr(bi, "_capture_web_profile_info", fake_capture)

    async with AsyncSessionLocal() as db:
        camp = await db.get(Campaign, cid)
        row = await db.get(ImportedProfile, rid)
        outcome, _ = await bi.resolve_and_store_bio_browser(row, camp, db, _FakeSession())
    assert outcome == "error"

    async with AsyncSessionLocal() as db:
        r = await db.get(ImportedProfile, rid)
        assert r.status == "error"
        n = await db.scalar(select(func.count()).select_from(Follower).where(Follower.campaign_id == cid))
        assert n == 0
