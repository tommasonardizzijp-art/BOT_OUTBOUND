"""E2E: campagna IMPORT creata dalla UI con bio_engine='browser' usa il BROWSER,
mai l'API instagrapi.

Simula il flusso reale del frontend (frontend/app/campaigns/new/page.tsx::handleSubmit):
  1. POST /campaigns            -> app.api.campaigns.create_campaign  (source_type=import, bio_engine=browser)
  2. POST .../import-profiles   -> store_imported_lines               (upload lista profili)
  3. assegnazione account scraping (CampaignAccount role='scraping')
  4. POST .../start-scrape      -> app.api.campaigns.start_scrape     ("click Avvia")
  5. worker ARQ                 -> app.workers.import_worker.resolve_imports_task

Poi guida il worker sullo STESSO ingresso runtime dell'ARQ e prova che:
  - ZERO chiamate instagrapi (user_info_by_username_v1) e ZERO ScrapingPool.build
    sul path browser (entrambi patchati per esplodere: il conteggio deve restare 0);
  - i Follower nascono `bio_scraped` col pk catturato dal web + bio + contatti;
  - le ImportedProfile -> 'resolved', la campagna termina 'ready' (messaging_enabled=True);
  - il dispatch DIFFERISCE: con bio_engine='api' lo stesso click prende il path API
    (ScrapingPool.build) e NON tocca il motore browser.

Confini mockati (SOLO il boundary browser/IG/Redis, mai il DB di test sqlite):
  BrowserSession, _capture_web_profile_info, _fetch_public_contact_inpage,
  human_profile_pause, maybe_micro_scroll, pick_session_cap, _soft_block_reset/_incr,
  + il boundary Redis della UI (enqueue_resolve, _check_redis_reachable).
"""
import uuid

import pytest
from sqlalchemy import func, select

from app.database import AsyncSessionLocal
from app.models.account import AccountStatus, InstagramAccount
from app.models.campaign import Campaign, CampaignStatus
from app.models.campaign_account import CampaignAccount
from app.models.follower import Follower, FollowerStatus
from app.models.imported_profile import ImportedProfile
from app.services import browser_import as bi
from app.services import import_resolver as ir


# --------------------------------------------------------------------------- #
# Fakes / helpers — mirror di tests/test_browser_import.py
# --------------------------------------------------------------------------- #
class _FakeSession:
    """Sessione Patchright finta: nessun browser reale, nessun IG. Traccia le
    aperture cosi' possiamo asserire che il motore browser e' davvero partito."""
    opens = 0

    def __init__(self, *a, **k):
        self.opened = False
        self.closed = False

    async def open(self):
        _FakeSession.opens += 1
        self.opened = True

    async def close(self):
        self.closed = True

    class _P:
        async def ensure_logged_in(self, account_id, allow_login=True):
            return None

        async def _get_page(self):
            return object()  # raw_page fittizio: _capture_* e' patchato, non lo usa

        async def browse_reels(self, *a, **k):
            return None

    page = _P()


async def _anoop(*a, **k):
    return None


async def _anoop_false(*a, **k):
    return False


def _web_user(pk, username, *, private=False, bio="", email=None):
    """Un dict `data.user` di web_profile_info come lo consuma web_user_to_shim."""
    return {
        "id": str(pk),
        "username": username,
        "full_name": f"{username} SRL",
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


async def _seed_scraping_account(cid: str) -> str:
    """Crea un InstagramAccount attivo e lo assegna alla campagna come 'scraping'."""
    acc_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        db.add(InstagramAccount(
            id=acc_id,
            username=f"scraper_{acc_id[:8]}",
            encrypted_password="x",
            status=AccountStatus.active,
            daily_message_limit=20,
        ))
        db.add(CampaignAccount(
            campaign_id=cid, account_id=acc_id, role="scraping", is_active=True,
        ))
        await db.commit()
    return acc_id


async def _ui_create_and_start(monkeypatch, *, engine: str, usernames: list[str]):
    """Riproduce create_campaign + import_profiles + assegnazione account + start-scrape,
    con SOLO il boundary Redis della UI mockato. Ritorna (cid, enqueue_calls)."""
    from app.api import campaigns as capi
    from app.services import work_enqueue as we
    from app.schemas.campaign import CampaignCreate
    from app.services.import_resolver import store_imported_lines

    # Boundary Redis della UI: nessun Redis reale.
    async def _redis_ok():
        return True
    monkeypatch.setattr(capi, "_check_redis_reachable", _redis_ok)

    enqueue_calls: list[str] = []

    async def _fake_enqueue_resolve(campaign_id):
        enqueue_calls.append(campaign_id)
        return True
    # start_scrape fa `from app.services.work_enqueue import enqueue_resolve` a runtime:
    # patchiamo l'attributo sul modulo sorgente.
    monkeypatch.setattr(we, "enqueue_resolve", _fake_enqueue_resolve)

    # 1) create_campaign — stesso payload del frontend (handleSubmit, source_type=import).
    payload = CampaignCreate(
        name=f"E2E import {engine}",
        source_type="import",
        target_username=None,
        scrape_mode="followers",
        bio_engine=engine,
        messaging_enabled=True,
        base_message_template="Ciao, ti scrivo per una collaborazione interessante.",
    )
    async with AsyncSessionLocal() as db:
        resp = await capi.create_campaign(payload, db)
    cid = resp.id
    assert resp.source_type == "import"
    assert resp.bio_engine == engine
    assert resp.status == CampaignStatus.draft

    # 2) import_profiles — upload lista (un profilo per riga).
    async with AsyncSessionLocal() as db:
        counts = await store_imported_lines(db, cid, "\n".join(usernames))
    assert counts["inserted"] == len(usernames)

    # 3) assegna account scraping attivo.
    await _seed_scraping_account(cid)

    # 4) start-scrape ("click Avvia"): transizione draft -> scraping + enqueue_resolve.
    async with AsyncSessionLocal() as db:
        started = await capi.start_scrape(cid, db)
    assert started.status == CampaignStatus.scraping
    assert enqueue_calls == [cid]  # la UI ha accodato ESATTAMENTE il resolve import

    return cid, enqueue_calls


def _install_api_tripwires(monkeypatch, *, api_calls: list, pool_builds: list, raising: bool):
    """Piazza i tripwire anti-API: instagrapi.Client.user_info_by_username_v1 e
    ScrapingPool.build. Registrano ogni chiamata; se `raising` esplodono anche
    (sul path browser NON devono mai essere toccati)."""
    from instagrapi import Client

    def _user_info_stub(*a, **k):
        api_calls.append(a[1:] if len(a) > 1 else a)
        raise AssertionError("instagrapi user_info_by_username_v1 chiamato sul path browser!")
    monkeypatch.setattr(Client, "user_info_by_username_v1", _user_info_stub)

    async def _build_stub(*a, **k):
        pool_builds.append(True)
        raise AssertionError("ScrapingPool.build chiamato sul path browser!")
    if raising:
        # import_resolver.ScrapingPool e' lo stesso oggetto-classe di scraping_pool.ScrapingPool
        monkeypatch.setattr(ir.ScrapingPool, "build", staticmethod(_build_stub))


# --------------------------------------------------------------------------- #
# E2E — path BROWSER: zero API, Follower bio_scraped, campagna ready
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_ui_import_browser_e2e_zero_api(monkeypatch):
    usernames = ["brandalpha", "brandbeta", "brandgamma"]
    web = {
        # email business dal web -> contatto ig_business
        "brandalpha": _web_user(831000001, "brandalpha",
                                 bio="Brand Alpha - moda sostenibile", email="alpha@brand.com"),
        # email SOLO nella bio -> contatto via regex
        "brandbeta": _web_user(831000002, "brandbeta",
                               bio="scrivimi a beta@brand.com per collab", email=None),
        # nessuna email; telefono arricchito via /info/ in-page (sotto)
        "brandgamma": _web_user(831000003, "brandgamma",
                                bio="solo brand, no contatti in bio", email=None),
    }

    # --- Tripwire anti-API: se instagrapi/ScrapingPool vengono toccati, lo sappiamo.
    api_calls: list = []
    pool_builds: list = []
    _install_api_tripwires(monkeypatch, api_calls=api_calls, pool_builds=pool_builds, raising=True)

    # --- Flusso UI reale (create + upload + assegna account + start).
    cid, enqueue_calls = await _ui_create_and_start(monkeypatch, engine="browser", usernames=usernames)

    # --- Mock del SOLO boundary browser/IG (nessun Patchright, nessun IG, nessun Redis).
    monkeypatch.setattr(bi, "BrowserSession", _FakeSession)
    monkeypatch.setattr(bi, "pick_session_cap", lambda *a, **k: 10)  # >> 3 pending -> drena tutto
    monkeypatch.setattr(bi, "human_profile_pause", _anoop)
    monkeypatch.setattr(bi, "maybe_micro_scroll", _anoop_false)
    monkeypatch.setattr(bi, "_soft_block_reset", _anoop)   # niente Redis nel test
    monkeypatch.setattr(bi, "_soft_block_incr", _anoop)
    monkeypatch.setattr(bi.settings, "bio_browser_contact_info_enabled", True)

    async def fake_capture(raw_page, username, timeout_s=8.0):
        return web[username]
    monkeypatch.setattr(bi, "_capture_web_profile_info", fake_capture)

    async def fake_info(raw_page, pk):
        # /info/ in-page: solo gamma ha un telefono business (public_phone_number + cc).
        if int(pk) == 831000003:
            return {"public_phone_number": "3401234567", "public_phone_country_code": "39"}
        return None
    monkeypatch.setattr(bi, "_fetch_public_contact_inpage", fake_info)

    _FakeSession.opens = 0

    # --- Fan-out: intercetta l'enqueue dei worker per-account (niente Redis reale).
    fanout_jobs: list = []

    class _FanoutRedis:
        async def exists(self, key):
            return 0

        async def delete(self, *keys):
            return None

        async def enqueue_job(self, task, *args, **kw):
            fanout_jobs.append((task, args))

        async def aclose(self):
            return None

    async def _fake_pool(*a, **k):
        return _FanoutRedis()
    monkeypatch.setattr(bi.arq, "create_pool", _fake_pool)

    # 5) worker ARQ di dispatch: resolve_imports_task -> fan-out (accoda i per-account).
    from app.workers.import_worker import resolve_imports_task
    await resolve_imports_task({}, cid)
    assert fanout_jobs and all(t == "browser_import_account_task" for t, _ in fanout_jobs)

    # 6) esegui i worker per-account: la VERA risoluzione via browser (una finestra/account).
    for _task, args in fanout_jobs:
        await bi.resolve_imports_browser_session(args[0], args[1])

    # ===================== ASSERZIONI =====================
    # (0) ZERO chiamate API instagrapi e ZERO ScrapingPool.build sul path browser.
    assert api_calls == [], f"instagrapi user_info chiamato sul path browser: {api_calls}"
    assert pool_builds == [], f"ScrapingPool.build chiamato sul path browser: {pool_builds}"
    # Il motore browser e' davvero partito (sessione aperta almeno una volta).
    assert _FakeSession.opens >= 1

    async with AsyncSessionLocal() as db:
        # (1) tutte le ImportedProfile -> 'resolved'
        rows = (await db.execute(
            select(ImportedProfile).where(ImportedProfile.campaign_id == cid)
        )).scalars().all()
        assert len(rows) == 3
        assert {r.status for r in rows} == {"resolved"}
        assert all(r.ig_user_id is not None for r in rows)

        # (2) 3 Follower, tutti bio_scraped, pk dal web capture + bio + contatti
        followers = {
            f.username: f for f in (await db.execute(
                select(Follower).where(Follower.campaign_id == cid)
            )).scalars().all()
        }
        assert set(followers) == {"brandalpha", "brandbeta", "brandgamma"}
        assert all(f.status == FollowerStatus.bio_scraped for f in followers.values())

        alpha = followers["brandalpha"]
        assert alpha.ig_user_id == 831000001               # pk dal web_profile_info
        assert alpha.biography == "Brand Alpha - moda sostenibile"
        assert alpha.follower_count == 1200
        assert alpha.email == "alpha@brand.com"            # email business dal web

        beta = followers["brandbeta"]
        assert beta.ig_user_id == 831000002
        assert beta.email == "beta@brand.com"              # email estratta dalla bio (regex)

        gamma = followers["brandgamma"]
        assert gamma.ig_user_id == 831000003
        assert gamma.phone == "+393401234567"              # telefono arricchito via /info/

        # (3) campagna terminata 'ready' (messaging_enabled=True), conteggi coerenti
        camp = await db.get(Campaign, cid)
        assert camp.status == CampaignStatus.ready
        assert camp.total_followers == 3
        assert camp.messages_pending == 3
        assert camp.scrape_outcome == "completed"


# --------------------------------------------------------------------------- #
# Controllo NEGATIVO — stesso click, bio_engine='api' -> path API (dispatch differisce)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_ui_import_api_dispatch_differs(monkeypatch):
    from app.services.scraping_pool import ScrapingPoolEmpty

    usernames = ["apibrand1", "apibrand2"]

    # Registra le due destinazioni possibili del dispatch.
    browser_calls: list = []
    pool_builds: list = []

    async def _browser_stub(campaign_id):
        browser_calls.append(campaign_id)
        raise AssertionError("il motore browser NON deve girare per bio_engine=api")
    monkeypatch.setattr(bi, "enqueue_browser_import_workers", _browser_stub)

    async def _build_stub(*a, **k):
        pool_builds.append(True)
        # short-circuit pulito: nessun login/instagrapi reale, campagna -> error
        raise ScrapingPoolEmpty("no accounts (test)")
    monkeypatch.setattr(ir.ScrapingPool, "build", staticmethod(_build_stub))

    # Tripwire instagrapi: non deve essere raggiunto (build fallisce prima).
    api_calls: list = []
    from instagrapi import Client

    def _user_info_stub(*a, **k):
        api_calls.append(a)
        raise AssertionError("instagrapi user_info non deve essere chiamato in questo test")
    monkeypatch.setattr(Client, "user_info_by_username_v1", _user_info_stub)

    cid, _ = await _ui_create_and_start(monkeypatch, engine="api", usernames=usernames)

    # Stesso ingresso worker del path browser.
    from app.workers.import_worker import resolve_imports_task
    await resolve_imports_task({}, cid)

    # Il dispatch e' andato all'API: build chiamato, browser MAI toccato, user_info mai raggiunto.
    assert pool_builds == [True], "il path API non ha chiamato ScrapingPool.build"
    assert browser_calls == [], f"il motore browser e' partito per bio_engine=api: {browser_calls}"
    assert api_calls == []

    async with AsyncSessionLocal() as db:
        camp = await db.get(Campaign, cid)
        # API path -> ScrapingPool.build -> ScrapingPoolEmpty -> campagna in error
        assert camp.status == CampaignStatus.error
        # nessun Follower creato: la risoluzione API non e' nemmeno partita
        n = await db.scalar(
            select(func.count()).select_from(Follower).where(Follower.campaign_id == cid)
        )
        assert n == 0
