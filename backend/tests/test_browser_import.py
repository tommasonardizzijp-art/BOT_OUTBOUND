"""Resolver import via BROWSER (bio_engine='browser' su source_type='import'), FAN-OUT.

Copre: (1) resolve_and_store_bio_browser (crea Follower da username, no API),
(2) il claim atomico status-flip (pending->resolving) + recupero stale,
(3) la mini-sessione per-account resolve_imports_browser_session (cap/defer, completamento,
soft-block/network, account fatale), (4) il fan-out enqueue + il dispatch in resolve_imports,
(5) il task ARQ per-account.
"""
from datetime import datetime, timedelta

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


class _FakeArqRedis:
    def __init__(self, in_progress=None):
        self.jobs = []
        self.deleted = []
        self._inprog = set(in_progress or [])

    async def exists(self, key):
        return 1 if key in self._inprog else 0

    async def delete(self, *keys):
        self.deleted.extend(keys)

    async def enqueue_job(self, task, *args, **kw):
        self.jobs.append((task, args, kw))

    async def aclose(self):
        return None


async def _anoop(*a, **k):
    return None


async def _anoop_false(*a, **k):
    return False


def _web_user(pk, username, *, private=False, bio="ciao bio", email=None):
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


def _use_fake_browser(monkeypatch, cap=5):
    monkeypatch.setattr(bi, "BrowserSession", _FakeSession)
    monkeypatch.setattr(bi, "pick_session_cap", lambda *a, **k: cap)
    monkeypatch.setattr(bi, "human_profile_pause", _anoop)
    monkeypatch.setattr(bi, "maybe_micro_scroll", _anoop_false)


# --------------------------------------------------------------------------- #
# resolve_and_store_bio_browser — crea il Follower da username (no API)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_resolve_and_store_creates_follower_public(monkeypatch):
    cid = await _mk_import_campaign()
    async with AsyncSessionLocal() as db:
        row = ImportedProfile(campaign_id=cid, raw_input="tgt", username="tgt", status="resolving")
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
        row = ImportedProfile(campaign_id=cid, raw_input="priv", username="priv", status="resolving")
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
        row = ImportedProfile(campaign_id=cid, raw_input="ghost", username="ghost", status="resolving")
        db.add(row)
        await db.commit()
        rid = row.id

    async def fake_capture(raw_page, username, timeout_s=8.0):
        return None
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
async def test_resolve_and_store_soft_block_not_marked(monkeypatch):
    cid = await _mk_import_campaign()
    async with AsyncSessionLocal() as db:
        row = ImportedProfile(campaign_id=cid, raw_input="rl", username="rl", status="resolving")
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
        assert r.status == "resolving"  # NON marcata: il chiamante rilascia il claim
        n = await db.scalar(select(func.count()).select_from(Follower).where(Follower.campaign_id == cid))
        assert n == 0


@pytest.mark.asyncio
async def test_resolve_and_store_dedup_no_duplicate(monkeypatch):
    cid = await _mk_import_campaign()
    async with AsyncSessionLocal() as db:
        db.add(Follower(campaign_id=cid, ig_user_id=700100003, username="dup",
                        status=FollowerStatus.bio_scraped))
        row = ImportedProfile(campaign_id=cid, raw_input="dup", username="dup", status="resolving")
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
        assert n == 1


@pytest.mark.asyncio
async def test_resolve_and_store_no_pk_error(monkeypatch):
    cid = await _mk_import_campaign()
    async with AsyncSessionLocal() as db:
        row = ImportedProfile(campaign_id=cid, raw_input="nopk", username="nopk", status="resolving")
        db.add(row)
        await db.commit()
        rid = row.id

    monkeypatch.setattr(bi.settings, "bio_browser_contact_info_enabled", False)

    async def fake_capture(raw_page, username, timeout_s=8.0):
        return {"username": username, "biography": "x"}  # SENZA 'id'
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
        row = ImportedProfile(campaign_id=cid, raw_input="g", username="g", status="resolving")
        db.add(row)
        await db.commit()
        rid = row.id

    async def fake_capture(raw_page, username, timeout_s=8.0):
        return {"__status": 404}
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


# --------------------------------------------------------------------------- #
# Claim atomico status-flip (pending -> resolving) + recupero stale
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_claim_flips_pending_to_resolving_and_exhausts():
    cid = await _mk_import_campaign(n_pending=2)
    async with AsyncSessionLocal() as db:
        r1 = await bi.claim_next_pending_import(db, cid, "acc-A")
        assert r1 is not None and r1.status == "resolving"
        r2 = await bi.claim_next_pending_import(db, cid, "acc-B")
        assert r2 is not None and r2.status == "resolving" and r2.id != r1.id
        r3 = await bi.claim_next_pending_import(db, cid, "acc-A")
        assert r3 is None  # nessun pending residuo (2 in resolving)


@pytest.mark.asyncio
async def test_claim_recovers_stale_resolving():
    cid = await _mk_import_campaign()
    async with AsyncSessionLocal() as db:
        old = datetime.utcnow() - timedelta(hours=2)
        row = ImportedProfile(campaign_id=cid, raw_input="s", username="s",
                              status="resolving", updated_at=old)
        db.add(row)
        await db.commit()
        rid = row.id
    async with AsyncSessionLocal() as db:
        claimed = await bi.claim_next_pending_import(db, cid, "acc-A")
        assert claimed is not None and claimed.id == rid and claimed.status == "resolving"


# --------------------------------------------------------------------------- #
# Mini-sessione per-account
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_session_resolves_up_to_cap_and_defers(monkeypatch):
    cid = await _mk_import_campaign(n_pending=5)
    _use_fake_browser(monkeypatch, cap=2)

    async def fake_resolve(row, campaign, db, session):
        row.status = "resolved"
        row.ig_user_id = 700200000 + int(row.username[1:])
        row.updated_at = datetime.utcnow()
        await db.commit()
        return "done", None
    monkeypatch.setattr(bi, "resolve_and_store_bio_browser", fake_resolve)

    defer = await bi.resolve_imports_browser_session(cid, "acc-A")
    assert isinstance(defer, int) and defer >= 60

    async with AsyncSessionLocal() as db:
        resolved = await db.scalar(
            select(func.count()).select_from(ImportedProfile).where(
                ImportedProfile.campaign_id == cid, ImportedProfile.status == "resolved"))
        assert resolved == 2  # esattamente il cap


@pytest.mark.asyncio
async def test_session_drains_pool_and_completes(monkeypatch):
    cid = await _mk_import_campaign(n_pending=3, messaging=True)
    _use_fake_browser(monkeypatch, cap=10)

    async def fake_resolve(row, campaign, db, session):
        row.status = "resolved"
        row.ig_user_id = 700300000 + int(row.username[1:])
        row.updated_at = datetime.utcnow()
        db.add(Follower(campaign_id=cid, ig_user_id=row.ig_user_id,
                        username=row.username, status=FollowerStatus.bio_scraped))
        await db.commit()
        return "done", None
    monkeypatch.setattr(bi, "resolve_and_store_bio_browser", fake_resolve)

    defer = await bi.resolve_imports_browser_session(cid, "acc-A")
    assert defer is None

    async with AsyncSessionLocal() as db:
        c = await db.get(Campaign, cid)
        assert c.status == CampaignStatus.ready
        assert c.total_followers == 3
        assert c.messages_pending == 3
        assert c.scrape_outcome == "completed"


@pytest.mark.asyncio
async def test_session_completes_when_no_pending(monkeypatch):
    cid = await _mk_import_campaign(n_pending=0, messaging=False)

    def _boom(*a, **k):
        raise AssertionError("BrowserSession non deve aprirsi senza pending")
    monkeypatch.setattr(bi, "BrowserSession", _boom)

    defer = await bi.resolve_imports_browser_session(cid, "acc-A")
    assert defer is None
    async with AsyncSessionLocal() as db:
        c = await db.get(Campaign, cid)
        assert c.status == CampaignStatus.completed  # messaging_enabled=False


@pytest.mark.asyncio
async def test_session_soft_block_releases_claim_and_backoff(monkeypatch):
    cid = await _mk_import_campaign(n_pending=3)
    _use_fake_browser(monkeypatch, cap=5)

    async def fake_resolve(row, campaign, db, session):
        return "soft_block", Exception("429")
    monkeypatch.setattr(bi, "resolve_and_store_bio_browser", fake_resolve)

    async def fake_incr(campaign_id, account_id):
        return 1
    monkeypatch.setattr(bi, "_soft_block_incr", fake_incr)

    defer = await bi.resolve_imports_browser_session(cid, "acc-A")
    assert isinstance(defer, int) and 900 <= defer <= 1800  # backoff
    async with AsyncSessionLocal() as db:
        c = await db.get(Campaign, cid)
        assert c.status == CampaignStatus.scraping
        # claim rilasciato: nessuna riga resta 'resolving', tutte tornano pending
        pend = await db.scalar(
            select(func.count()).select_from(ImportedProfile).where(
                ImportedProfile.campaign_id == cid, ImportedProfile.status == "pending"))
        resolving = await db.scalar(
            select(func.count()).select_from(ImportedProfile).where(
                ImportedProfile.campaign_id == cid, ImportedProfile.status == "resolving"))
        assert pend == 3 and resolving == 0


@pytest.mark.asyncio
async def test_session_soft_block_threshold_pauses(monkeypatch):
    cid = await _mk_import_campaign(n_pending=3)
    _use_fake_browser(monkeypatch, cap=5)

    async def fake_resolve(row, campaign, db, session):
        return "soft_block", Exception("429")
    monkeypatch.setattr(bi, "resolve_and_store_bio_browser", fake_resolve)

    thr = bi.settings.bio_browser_soft_block_pause_threshold

    async def fake_incr(campaign_id, account_id):
        return thr
    monkeypatch.setattr(bi, "_soft_block_incr", fake_incr)

    defer = await bi.resolve_imports_browser_session(cid, "acc-A")
    assert defer is None
    async with AsyncSessionLocal() as db:
        c = await db.get(Campaign, cid)
        assert c.status == CampaignStatus.paused


@pytest.mark.asyncio
async def test_session_network_releases_claim_defers_180(monkeypatch):
    cid = await _mk_import_campaign(n_pending=2)
    _use_fake_browser(monkeypatch, cap=5)

    async def net(row, campaign, db, session):
        return "network", Exception("net:: down")
    monkeypatch.setattr(bi, "resolve_and_store_bio_browser", net)

    defer = await bi.resolve_imports_browser_session(cid, "acc-A")
    assert defer == 180
    async with AsyncSessionLocal() as db:
        pend = await db.scalar(
            select(func.count()).select_from(ImportedProfile).where(
                ImportedProfile.campaign_id == cid, ImportedProfile.status == "pending"))
        assert pend == 2  # claim rilasciato, nessuna riga bruciata


@pytest.mark.asyncio
async def test_session_resolve_exception_marks_error_and_advances(monkeypatch):
    cid = await _mk_import_campaign(n_pending=2)
    _use_fake_browser(monkeypatch, cap=5)

    async def boom(row, campaign, db, session):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(bi, "resolve_and_store_bio_browser", boom)

    defer = await bi.resolve_imports_browser_session(cid, "acc-A")
    assert defer is None  # tutte error -> pool esaurito -> completa
    async with AsyncSessionLocal() as db:
        errs = await db.scalar(
            select(func.count()).select_from(ImportedProfile).where(
                ImportedProfile.campaign_id == cid, ImportedProfile.status == "error"))
        assert errs == 2
        c = await db.get(Campaign, cid)
        assert c.status == CampaignStatus.ready


@pytest.mark.asyncio
async def test_session_backstop_short_defer(monkeypatch):
    cap = 2
    max_it = cap * bi.MAX_SESSION_ITERATIONS_MULTIPLIER
    cid = await _mk_import_campaign(n_pending=max_it + 5)
    _use_fake_browser(monkeypatch, cap=cap)

    calls = {"n": 0}

    async def nf(row, campaign, db, session):
        calls["n"] += 1
        row.status = "not_found"  # terminale -> avanza (evita re-claim infinito)
        row.updated_at = datetime.utcnow()
        await db.commit()
        return "not_found", None
    monkeypatch.setattr(bi, "resolve_and_store_bio_browser", nf)

    defer = await bi.resolve_imports_browser_session(cid, "acc-A")
    assert defer == 60          # backstop -> defer breve
    assert calls["n"] <= max_it


@pytest.mark.asyncio
async def test_session_fatal_account_isolates_and_pauses(monkeypatch):
    cid = await _mk_import_campaign(n_pending=2)
    monkeypatch.setattr(bi, "BrowserSession", _FatalLoginSession)

    defer = await bi.resolve_imports_browser_session(cid, "acc-fatal")
    assert defer is None  # isolato, non retry
    async with AsyncSessionLocal() as db:
        c = await db.get(Campaign, cid)
        assert c.status == CampaignStatus.paused


# --------------------------------------------------------------------------- #
# Fan-out enqueue
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_enqueue_fans_out_per_account(monkeypatch):
    async def two_accounts(_cid):
        return [("acc-A", "a"), ("acc-B", "b")]
    monkeypatch.setattr(bi, "_scraping_accounts_of_campaign", two_accounts)

    fake = _FakeArqRedis()

    async def fake_pool(*a, **k):
        return fake
    monkeypatch.setattr(bi.arq, "create_pool", fake_pool)

    n = await bi.enqueue_browser_import_workers("cid1")
    assert n == 2
    assert len(fake.jobs) == 2
    assert all(j[0] == "browser_import_account_task" for j in fake.jobs)
    accs = {j[1][1] for j in fake.jobs}  # args = (campaign_id, account_id)
    assert accs == {"acc-A", "acc-B"}


@pytest.mark.asyncio
async def test_enqueue_skips_account_already_in_progress(monkeypatch):
    async def two_accounts(_cid):
        return [("acc-A", "a"), ("acc-B", "b")]
    monkeypatch.setattr(bi, "_scraping_accounts_of_campaign", two_accounts)

    # acc-A gia' in esecuzione: non ri-accodato, ma contato
    inprog = {f"arq:in-progress:{bi.browser_import_job_id('cid1', 'acc-A')}"}
    fake = _FakeArqRedis(in_progress=inprog)

    async def fake_pool(*a, **k):
        return fake
    monkeypatch.setattr(bi.arq, "create_pool", fake_pool)

    n = await bi.enqueue_browser_import_workers("cid1")
    assert n == 2                       # 1 gia' in corso + 1 nuovo
    assert len(fake.jobs) == 1          # solo acc-B accodato
    assert fake.jobs[0][1][1] == "acc-B"


@pytest.mark.asyncio
async def test_enqueue_no_accounts_returns_zero(monkeypatch):
    async def no_accounts(_cid):
        return []
    monkeypatch.setattr(bi, "_scraping_accounts_of_campaign", no_accounts)
    n = await bi.enqueue_browser_import_workers("cid1")
    assert n == 0


# --------------------------------------------------------------------------- #
# Dispatch in resolve_imports (import_resolver)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_resolve_imports_dispatches_to_browser_fanout(monkeypatch):
    from app.services import import_resolver as ir
    cid = await _mk_import_campaign(engine="browser", n_pending=1)

    called = {}

    async def fake_enqueue(campaign_id):
        called["cid"] = campaign_id
        return 2
    monkeypatch.setattr(bi, "enqueue_browser_import_workers", fake_enqueue)

    async def fake_build(*a, **k):
        raise AssertionError("il path API non deve girare per bio_engine=browser")
    monkeypatch.setattr(ir.ScrapingPool, "build", staticmethod(fake_build))

    result = await ir.resolve_imports(cid)
    assert result is None
    assert called["cid"] == cid
    async with AsyncSessionLocal() as db:
        c = await db.get(Campaign, cid)
        assert c.status == CampaignStatus.scraping  # i worker girano a parte


@pytest.mark.asyncio
async def test_resolve_imports_browser_no_account_sets_error(monkeypatch):
    from app.services import import_resolver as ir
    cid = await _mk_import_campaign(engine="browser", n_pending=1)

    async def fake_enqueue_zero(campaign_id):
        return 0
    monkeypatch.setattr(bi, "enqueue_browser_import_workers", fake_enqueue_zero)

    result = await ir.resolve_imports(cid)
    assert result is None
    async with AsyncSessionLocal() as db:
        c = await db.get(Campaign, cid)
        assert c.status == CampaignStatus.error
        assert c.scrape_outcome == "scrape_no_account"


@pytest.mark.asyncio
async def test_resolve_imports_api_path_when_engine_api(monkeypatch):
    from app.services import import_resolver as ir
    from app.services.scraping_pool import ScrapingPoolEmpty
    cid = await _mk_import_campaign(engine="api", n_pending=1)

    async def fake_enqueue(campaign_id):
        raise AssertionError("il fan-out browser non deve girare per bio_engine=api")
    monkeypatch.setattr(bi, "enqueue_browser_import_workers", fake_enqueue)

    async def fake_build(*a, **k):
        raise ScrapingPoolEmpty("no accounts (test)")
    monkeypatch.setattr(ir.ScrapingPool, "build", staticmethod(fake_build))

    result = await ir.resolve_imports(cid)
    assert result is None
    async with AsyncSessionLocal() as db:
        c = await db.get(Campaign, cid)
        assert c.status == CampaignStatus.error  # API path -> pool build -> ScrapingPoolEmpty


# --------------------------------------------------------------------------- #
# Guardia single-job del job di dispatch resolve:{cid} (invariata dalla PR base)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_enqueue_resolve_skips_when_in_progress():
    from app.services.work_enqueue import _enqueue_resolve_with_redis
    r = _FakeArqRedis(in_progress={"arq:in-progress:resolve:cid1"})
    ok = await _enqueue_resolve_with_redis(r, "cid1")
    assert ok is False
    assert r.jobs == []
    assert not any("in-progress" in k for k in r.deleted)


@pytest.mark.asyncio
async def test_enqueue_resolve_runs_when_free():
    from app.services.work_enqueue import _enqueue_resolve_with_redis
    r = _FakeArqRedis()
    ok = await _enqueue_resolve_with_redis(r, "cid1")
    assert ok is True
    assert len(r.jobs) == 1
    assert not any("in-progress" in k for k in r.deleted)


# --------------------------------------------------------------------------- #
# Task ARQ per-account onora il defer
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_import_account_task_raises_retry_on_defer(monkeypatch):
    from app.workers import task_queue as tq
    from arq.worker import Retry

    async def fake_session(cid, aid):
        return 300
    monkeypatch.setattr("app.services.browser_import.resolve_imports_browser_session", fake_session)

    with pytest.raises(Retry):
        await tq.browser_import_account_task({}, "cid", "acc")


@pytest.mark.asyncio
async def test_import_account_task_no_retry_when_none(monkeypatch):
    from app.workers import task_queue as tq

    async def fake_session(cid, aid):
        return None
    monkeypatch.setattr("app.services.browser_import.resolve_imports_browser_session", fake_session)

    await tq.browser_import_account_task({}, "cid", "acc")  # non solleva
