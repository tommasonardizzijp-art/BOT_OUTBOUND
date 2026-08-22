"""
Adversarial tests for the inbox-engine switch in update_campaign.

Il contratto e' cambiato (fix 22/08/2026): il cambio engine NON azzera piu'
nessun cursore. I due motori hanno gia' colonne separate — l'API scrive
`inbox_deep_cursor`/`scrape_cursor`, il browser `inbox_cursor_at` (migration
033) — quindi non c'e' nessun token da invalidare, e l'azzeramento era una
perdita secca: cancellava la frontiera della discesa e non metteva niente al
suo posto. Misurato su `PRIMERO ADV3 DM X VDF`, che ha dovuto ri-attraversare
1.200 conversazioni gia' raccolte (60 pagine) per tornare dov'era.

Endpoint state-gating via TestClient + SQLite fixture (module-scoped temp DB,
dependency_overrides per get_db + get_current_user). L'endpoint e' PUT /{id},
non PATCH; `inbox_engine` e' validato dallo schema con pattern
'^(browser|api)$', quindi i valori sporchi ('API', '', None) sono respinti da
Pydantic (422) prima di arrivare alla logica.
"""

import asyncio
import os
import tempfile
import uuid
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Register all ORM tables on Base.metadata.
import app.models.account  # noqa: F401
import app.models.activity_log  # noqa: F401
import app.models.campaign_account  # noqa: F401
import app.models.follower  # noqa: F401
import app.models.global_contact  # noqa: F401
import app.models.imported_profile  # noqa: F401
import app.models.message  # noqa: F401
import app.models.user  # noqa: F401

from app.database import Base, get_db
from app.models.campaign import Campaign, CampaignStatus
from app.models.user import User
from app.utils.auth_deps import get_current_user


# ============================================================================
# PART B — endpoint state-gating
# ============================================================================

# ---------- Fixtures (module-scoped temp SQLite, mirrors guard adversarial) --

@pytest.fixture(scope="module")
def _temp_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="e2e_engine_switch_")
    os.close(fd)
    url = f"sqlite+aiosqlite:///{path}"
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    yield engine, session_factory

    async def _dispose():
        await engine.dispose()

    asyncio.run(_dispose())
    try:
        os.remove(path)
    except OSError:
        pass


@pytest.fixture(scope="module")
def client(_temp_db):
    engine, session_factory = _temp_db

    async def _override_get_db():
        async with session_factory() as session:
            try:
                yield session
            finally:
                await session.close()

    def _override_get_current_user():
        return User(
            id="00000000-0000-0000-0000-000000000002",
            email="admin@test.local",
            password_hash="x",
            role="admin",
            is_active=True,
            created_at=datetime.utcnow(),
        )

    from app.main import app

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _override_get_current_user

    from fastapi.testclient import TestClient

    c = TestClient(app, raise_server_exceptions=True)
    yield c

    app.dependency_overrides.clear()


def _run(session_factory, coro_fn):
    """Run an async DB helper synchronously (mirrors existing helpers)."""
    async def _wrap():
        async with session_factory() as db:
            return await coro_fn(db)
    return asyncio.run(_wrap())


def _make_campaign(
    *,
    name: str,
    status: CampaignStatus,
    scrape_cursor: str | None = None,
    inbox_deep_cursor: str | None = None,
    inbox_cursor_at: datetime | None = None,
    inbox_engine: str = "browser",
    scrape_mode: str = "dm_threads",
    messaging_enabled: bool = False,
    bio_engine: str = "api",
    enrichment_level: str = "none",
) -> Campaign:
    return Campaign(
        id=str(uuid.uuid4()),
        name=name,
        source_type="scrape",
        target_username="target_user",
        scrape_mode=scrape_mode,
        inbox_engine=inbox_engine,
        scrape_cursor=scrape_cursor,
        inbox_deep_cursor=inbox_deep_cursor,
        inbox_cursor_at=inbox_cursor_at,
        status=status,
        messaging_enabled=messaging_enabled,
        bio_engine=bio_engine,
        enrichment_level=enrichment_level,
    )


# ---------- B-1: il cambio engine CONSERVA tutti e tre i segnalibri ----------

def test_engine_switch_preserves_every_cursor(client, _temp_db):
    """
    Il cuore del fix. Campagna paused con i segnalibri di ENTRAMBI i motori
    valorizzati; PUT inbox_engine='api' → 200 e nessuno dei tre viene toccato.

    Perche' i tre insieme e non solo quello dell'API: il vecchio codice
    azzerava `scrape_cursor` e `inbox_deep_cursor` e lasciava intatto
    `inbox_cursor_at`, cioe' trattava come "token dello stesso engine" tre
    colonne che engine dello stesso tipo non sono. Se un giorno qualcuno
    reintroduce l'azzeramento su una sola di esse, questo test lo prende.

    `inbox_deep_cursor` non e' esposto in CampaignResponse (e non deve
    esserlo: e' stato interno del worker), quindi si verifica leggendo la
    riga dal DB e non il corpo della risposta.
    """
    _, sf = _temp_db
    frontiera = '{"cursor_thread_v2_id":1395122721987860,"cursor_timestamp_seconds":1768837439}'
    quando = datetime(2026, 1, 19, 15, 43, 59)
    camp = _make_campaign(
        name="B1-switch-conserva-i-segnalibri",
        status=CampaignStatus.paused,
        scrape_cursor=frontiera,
        inbox_deep_cursor=frontiera,
        inbox_cursor_at=quando,
        inbox_engine="browser",
    )
    camp_id = camp.id

    async def _seed(db):
        db.add(camp)
        await db.commit()

    _run(sf, _seed)

    resp = client.put(
        f"/api/campaigns/{camp_id}",
        json={"inbox_engine": "api"},
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["inbox_engine"] == "api", f"inbox_engine non aggiornato: {body['inbox_engine']}"
    assert body["scrape_cursor"] == frontiera, (
        f"DIFETTO: il cambio engine ha azzerato scrape_cursor. Got: {body['scrape_cursor']!r}"
    )

    async def _rileggi(db):
        row = await db.get(Campaign, camp_id)
        return row.inbox_deep_cursor, row.inbox_cursor_at

    deep, cursor_at = _run(sf, _rileggi)
    assert deep == frontiera, (
        f"DIFETTO: il cambio engine ha azzerato la frontiera della discesa. Got: {deep!r}"
    )
    assert cursor_at == quando, (
        f"DIFETTO: il cambio engine ha toccato il segnalibro del browser. Got: {cursor_at!r}"
    )


# ---------- B-1b: il cambio engine lascia una traccia ------------------------

def test_engine_switch_scrive_activity_log(client, _temp_db):
    """
    Un'azione che cambia il comportamento del motore deve lasciare una traccia
    permanente. Gli eventi della UI vivono su Redis con scadenza 24h: senza
    questo log, a 48h di distanza non c'e' modo di sapere se un engine e' stato
    cambiato — ed e' esattamente la domanda a cui non si e' potuto rispondere
    indagando il caso PRIMERO del 22/08/2026.
    """
    _, sf = _temp_db
    camp = _make_campaign(
        name="B1b-switch-lascia-traccia",
        status=CampaignStatus.paused,
        inbox_engine="browser",
    )
    camp_id = camp.id

    async def _seed(db):
        db.add(camp)
        await db.commit()

    _run(sf, _seed)

    resp = client.put(f"/api/campaigns/{camp_id}", json={"inbox_engine": "api"})
    assert resp.status_code == 200, resp.text

    async def _logs(db):
        from app.models.activity_log import ActivityLog
        from sqlalchemy import select as _select
        rows = (await db.execute(
            _select(ActivityLog).where(ActivityLog.campaign_id == camp_id)
        )).scalars().all()
        return [(r.action, r.details) for r in rows]

    logs = _run(sf, _logs)
    switch = [d for a, d in logs if a == "inbox_engine_cambiato"]
    assert len(switch) == 1, f"DIFETTO: atteso 1 log di cambio engine, trovati {logs}"
    assert '"browser"' in (switch[0] or "") and '"api"' in (switch[0] or ""), (
        f"DIFETTO: il log non dice da quale engine a quale. details={switch[0]!r}"
    )


# ---------- B-2: same engine → cursor must NOT be reset ---------------------

def test_same_engine_patch_preserves_cursor(client, _temp_db):
    """
    dm_threads, paused, scrape_cursor='ABC', inbox_engine='browser'.
    PUT inbox_engine='browser' (no change) → 200, scrape_cursor still 'ABC'.

    This is the key idempotency contract: re-setting the same engine must not
    destroy progress. If cursor is reset here, a UI that always sends the full
    update payload would silently lose the scraping position.

    bio_engine/enrichment_level are seeded 'browser'/'contacts' (not the model
    default 'api'/'none') so the final combo stays valid under the Task-7 gate
    on valida_combinazione_motori — this test is about cursor idempotency, not
    about the gate, and must not trip it as a side effect.
    """
    _, sf = _temp_db
    camp = _make_campaign(
        name="B2-same-engine-keeps-cursor",
        status=CampaignStatus.paused,
        scrape_cursor="ABC",
        inbox_engine="browser",
        bio_engine="browser",
        enrichment_level="contacts",
    )
    camp_id = camp.id

    async def _seed(db):
        db.add(camp)
        await db.commit()

    _run(sf, _seed)

    resp = client.put(
        f"/api/campaigns/{camp_id}",
        json={"inbox_engine": "browser"},
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["inbox_engine"] == "browser", f"inbox_engine changed unexpectedly: {body['inbox_engine']}"
    assert body["scrape_cursor"] == "ABC", (
        f"DEFECT: cursor was reset even though engine did not change. "
        f"Got: {body['scrape_cursor']!r} (expected 'ABC')."
    )

    # Nessun cambio, nessuna traccia: un log a ogni salvataggio riempirebbe di
    # rumore proprio la tabella che serve a rispondere "l'engine e' stato
    # cambiato?", e la UI puo' rimandare lo stesso valore a ogni refresh.
    async def _logs(db):
        from app.models.activity_log import ActivityLog
        from sqlalchemy import select as _select
        rows = (await db.execute(
            _select(ActivityLog).where(
                ActivityLog.campaign_id == camp_id,
                ActivityLog.action == "inbox_engine_cambiato",
            )
        )).scalars().all()
        return len(rows)

    assert _run(sf, _logs) == 0, "DIFETTO: loggato un cambio engine che non e' avvenuto"


# ---------- B-3: active states → engine switch must be blocked (400) --------

@pytest.mark.parametrize("blocked_status", [
    CampaignStatus.listing,
    CampaignStatus.running,
    CampaignStatus.scraping,
])
def test_engine_switch_blocked_while_active(client, _temp_db, blocked_status):
    """
    Active states (listing / running / scraping) must reject inbox_engine change
    with 400. Changing engine mid-run would cause the worker to pick up a cursor
    from the wrong engine type on the very next iteration.
    """
    _, sf = _temp_db
    camp = _make_campaign(
        name=f"B3-blocked-{blocked_status.value}",
        status=blocked_status,
        scrape_cursor="XYZ",
        inbox_engine="browser",
    )
    camp_id = camp.id

    async def _seed(db):
        db.add(camp)
        await db.commit()

    _run(sf, _seed)

    resp = client.put(
        f"/api/campaigns/{camp_id}",
        json={"inbox_engine": "api"},
    )
    assert resp.status_code == 400, (
        f"DEFECT: engine switch allowed in status={blocked_status.value!r}. "
        f"Got {resp.status_code}: {resp.text}. "
        f"Must return 400 — engine switch while active corrupts the cursor."
    )
    detail = resp.json().get("detail", "")
    # Check the right guard fired, not some unrelated 400.
    assert any(kw in detail.lower() for kw in ("engine", "draft", "paused", "ferma")), (
        f"400 came from an unexpected guard (not the engine-switch guard): {detail}"
    )


# ---------- B-4: additional active states that must also be blocked ----------

@pytest.mark.parametrize("blocked_status", [
    CampaignStatus.listing_break,
    CampaignStatus.scraping_break,
    CampaignStatus.scraping_and_running,
])
def test_engine_switch_blocked_in_break_and_parallel_states(client, _temp_db, blocked_status):
    """
    Break and parallel-run states are not in the allowed set (draft/ready/paused/error).
    Changing engine here would be just as dangerous as during active scraping.
    """
    _, sf = _temp_db
    camp = _make_campaign(
        name=f"B4-blocked-{blocked_status.value}",
        status=blocked_status,
        scrape_cursor="DEF",
        inbox_engine="browser",
    )
    camp_id = camp.id

    async def _seed(db):
        db.add(camp)
        await db.commit()

    _run(sf, _seed)

    resp = client.put(
        f"/api/campaigns/{camp_id}",
        json={"inbox_engine": "api"},
    )
    assert resp.status_code == 400, (
        f"DEFECT: engine switch allowed in status={blocked_status.value!r}. "
        f"Got {resp.status_code}: {resp.text}."
    )


# ---------- B-5: allowed states (draft, ready, error) ------------------------

@pytest.mark.parametrize("allowed_status", [
    CampaignStatus.draft,
    CampaignStatus.ready,
    CampaignStatus.error,
])
def test_engine_switch_allowed_in_stopped_states(client, _temp_db, allowed_status):
    """
    draft / ready / error must allow inbox_engine changes (→ 200).
    These are the safe states where no worker holds a cursor.

    'error' was previously blocked by the outer gate (lines 259-264) because it
    was not in {draft, ready, paused}. Fixed by adding 'inbox_engine' to
    always_editable so it bypasses the outer gate and reaches its own inner guard
    (line 285) which correctly lists draft/ready/paused/error as allowed.
    """
    _, sf = _temp_db
    camp = _make_campaign(
        name=f"B5-allowed-{allowed_status.value}",
        status=allowed_status,
        scrape_cursor=None,
        inbox_engine="browser",
    )
    camp_id = camp.id

    async def _seed(db):
        db.add(camp)
        await db.commit()

    _run(sf, _seed)

    resp = client.put(
        f"/api/campaigns/{camp_id}",
        json={"inbox_engine": "api"},
    )
    assert resp.status_code == 200, (
        f"Expected 200 for status={allowed_status.value!r}, "
        f"got {resp.status_code}: {resp.text}"
    )
    assert resp.json()["inbox_engine"] == "api"


# ---------- B-6: engine switch does NOT clobber other fields -----------------

def test_engine_switch_does_not_clobber_name(client, _temp_db):
    """
    Set name='Original Name' first. Then PUT inbox_engine='api' only.
    The name must survive — update_campaign applies fields individually
    (if data.name is not None) so an omitted name must not be cleared.

    bio_engine/enrichment_level are set to 'browser'/'contacts' at creation
    so inbox_engine='browser' is a valid combo under the Task-7 gate — this
    test is about name preservation, not about the gate.
    """
    _, sf = _temp_db

    # Create via API so we get a real campaign with a valid name.
    resp_create = client.post(
        "/api/campaigns",
        json={
            "name": "Original Name",
            "source_type": "scrape",
            "target_username": "some_target",
            "messaging_enabled": False,
            "inbox_engine": "browser",
            "bio_engine": "browser",
            "enrichment_level": "contacts",
        },
    )
    assert resp_create.status_code == 201, resp_create.text
    camp_id = resp_create.json()["id"]

    # Now switch engine only (no name in payload).
    resp_update = client.put(
        f"/api/campaigns/{camp_id}",
        json={"inbox_engine": "api"},
    )
    assert resp_update.status_code == 200, resp_update.text
    body = resp_update.json()
    assert body["inbox_engine"] == "api", f"inbox_engine not updated: {body['inbox_engine']}"
    assert body["name"] == "Original Name", (
        f"DEFECT: name was clobbered by an engine-only update. "
        f"Got: {body['name']!r}"
    )


def test_engine_switch_does_not_clobber_template(client, _temp_db):
    """
    Create campaign with base_message_template set. Then PUT inbox_engine only.
    Template must survive — base_message_template uses model_fields_set guard
    so it is only written if explicitly included in the request.

    bio_engine/enrichment_level are set to 'browser'/'contacts' at creation
    so inbox_engine='browser' is a valid combo under the Task-7 gate — this
    test is about template preservation, not about the gate.
    """
    _, sf = _temp_db

    resp_create = client.post(
        "/api/campaigns",
        json={
            "name": "Template Survive Test",
            "source_type": "scrape",
            "target_username": "some_target",
            "messaging_enabled": True,
            "base_message_template": "Hello this is my outreach message for you",
            "inbox_engine": "browser",
            "bio_engine": "browser",
            "enrichment_level": "contacts",
        },
    )
    assert resp_create.status_code == 201, resp_create.text
    camp_id = resp_create.json()["id"]
    original_template = resp_create.json()["base_message_template"]

    resp_update = client.put(
        f"/api/campaigns/{camp_id}",
        json={"inbox_engine": "api"},
    )
    assert resp_update.status_code == 200, resp_update.text
    body = resp_update.json()
    assert body["inbox_engine"] == "api"
    assert body["base_message_template"] == original_template, (
        f"DEFECT: template clobbered by engine-only update. "
        f"Got: {body['base_message_template']!r}, expected: {original_template!r}"
    )


# ---------- B-7: completed state → engine switch must be blocked -------------

def test_engine_switch_blocked_in_completed_state(client, _temp_db):
    """
    completed is not in the allowed set (draft/ready/paused/error).
    A completed campaign has no cursor to protect, but the guard should
    still fire consistently — completed campaigns can only be updated for
    messaging fields, not engine settings.

    Note: update_campaign has a special path for completed campaigns:
    it allows certain 'completed_message_fields' but inbox_engine is NOT
    in that set, so the outer status guard fires and returns 400.
    """
    _, sf = _temp_db
    camp = _make_campaign(
        name="B7-completed-blocked",
        status=CampaignStatus.completed,
        scrape_cursor=None,
        inbox_engine="browser",
    )
    camp_id = camp.id

    async def _seed(db):
        db.add(camp)
        await db.commit()

    _run(sf, _seed)

    resp = client.put(
        f"/api/campaigns/{camp_id}",
        json={"inbox_engine": "api"},
    )
    assert resp.status_code == 400, (
        f"DEFECT: engine switch allowed on completed campaign. "
        f"Got {resp.status_code}: {resp.text}"
    )


# ---------- B-8: cursor is reset even when engine switch happens in 'ready' --

def test_engine_switch_in_ready_conserva_il_cursore(client, _temp_db):
    """
    Una campagna in 'ready' porta il cursore del giro precedente. Il cambio
    engine in ready lo deve CONSERVARE: e' lo stato in cui una campagna inbox
    passa la maggior parte della sua vita fra un giro e l'altro, quindi e'
    proprio qui che l'azzeramento faceva il danno peggiore.

    bio_engine/enrichment_level are seeded 'browser'/'contacts' up front so
    that switching inbox_engine to 'browser' lands on a combo the Task-7 gate
    (valida_combinazione_motori) accepts — this test is about the cursor,
    not about the gate.
    """
    _, sf = _temp_db
    camp = _make_campaign(
        name="B8-ready-cursore-conservato",
        status=CampaignStatus.ready,
        scrape_cursor="LEFTOVER_CURSOR",
        inbox_engine="api",
        bio_engine="browser",
        enrichment_level="contacts",
    )
    camp_id = camp.id

    async def _seed(db):
        db.add(camp)
        await db.commit()

    _run(sf, _seed)

    resp = client.put(
        f"/api/campaigns/{camp_id}",
        json={"inbox_engine": "browser"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["inbox_engine"] == "browser"
    assert body["scrape_cursor"] == "LEFTOVER_CURSOR", (
        f"DIFETTO: il cambio engine in 'ready' ha azzerato il cursore. "
        f"Got: {body['scrape_cursor']!r}"
    )
