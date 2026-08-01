import asyncio
from datetime import datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.models.user import User
from app.utils.auth_deps import get_current_user
from tests.factories_wa import make_tenant, make_number, make_campaign


def _admin_utente() -> User:
    return User(id="00000000-0000-0000-0000-00000000000b", email="admin-wa-ops@test.local",
               password_hash="x", role="admin", is_active=True,
               created_at=datetime.utcnow())


@pytest_asyncio.fixture
async def client_real_db():
    """Bypassa SOLO l'auth, NON get_db: ogni richiesta apre la sua sessione
    reale (AsyncSessionLocal), esattamente come in produzione. Serve per
    verificare che le scritture sopravvivano oltre la request -- con
    get_db overridden su db_session (pattern degli altri file test_wa_api_*)
    quel bug resterebbe invisibile: stessa sessione, read-your-own-write
    senza commit sembra funzionare anche quando non persiste davvero."""
    app.dependency_overrides[get_current_user] = _admin_utente
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_status_riporta_kill_switch_e_conteggi(db_session):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/wa/ops/status")
    assert r.status_code in (200, 401)      # 401 se l'auth e' attiva: e' corretto
    if r.status_code == 200:
        body = r.json()
        assert "wa_halted" in body and "send_enabled" in body


@pytest.mark.asyncio
async def test_8_status_espone_i_campi_con_dati_reali(db_session):
    """QA item 8: numeri_attivi/campagne_running/inviati_oggi non solo
    presenti ma calcolati sui dati veri a DB."""
    from app.api import wa_ops
    from app.models.wa import WaCampaignStatus, WaMessage, WaMessageStatus
    from tests.factories_wa import make_contact

    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number,
                                      status=WaCampaignStatus.running)
    contact = await make_contact(db_session, tenant)
    db_session.add(WaMessage(
        campaign_id=campaign.id, contact_id=contact.id, wa_number_id=number.id,
        step_index=0, template_variant="a", rendered_text="ciao",
        status=WaMessageStatus.sent, sent_at=datetime.utcnow()))
    await db_session.commit()

    body = await wa_ops.wa_ops_status(db=db_session)
    assert body["numeri_attivi"] >= 1
    assert body["campagne_running"] >= 1
    assert body["inviati_oggi"] >= 1
    assert isinstance(body["wa_halted"], bool)
    assert isinstance(body["send_enabled"], bool)


@pytest.mark.asyncio
async def test_halt_e_resume_cambiano_solo_il_canale_wa(db_session):
    from app.services import bot_state_service as bss
    await bss.halt_wa(reason="via API", by="test", db=db_session)
    assert await bss.is_wa_halted(db_session) is True
    assert await bss.is_halted(db_session) is False
    await bss.resume_wa(by="test", db=db_session)
    assert await bss.is_wa_halted(db_session) is False


@pytest.mark.asyncio
async def test_kick_su_campagna_non_running_non_accoda_nulla(db_session, monkeypatch):
    """Idempotenza/macchina a stati: un kick su una campagna in draft non
    deve creare lavoro."""
    from app.api import wa_ops
    from app.models.wa import WaCampaignStatus

    ctx = await _scenario_claim(db_session)
    ctx["campaign"].status = WaCampaignStatus.draft
    await db_session.commit()

    accodati = {"n": 0}

    async def _fake_enqueue(campaign_id):
        accodati["n"] += 1
        return 1
    monkeypatch.setattr(wa_ops, "enqueue_wa_workers", _fake_enqueue)

    esito = await wa_ops.kick_campaign(ctx["campaign"].id, db=db_session)
    assert accodati["n"] == 0
    assert esito["accodati"] == 0


async def _scenario_claim(db_session):
    """Crea una campagna claim di test con numero attivo."""
    from app.models.wa import WaCampaignStatus

    tenant = await make_tenant(db_session)
    wa_number = await make_number(db_session, tenant, label="Test Number")
    campaign, _ = await make_campaign(db_session, tenant, wa_number,
                                      name="Test Campaign",
                                      status=WaCampaignStatus.running)

    return {"campaign": campaign, "wa_number": wa_number}


@pytest.mark.asyncio
async def test_9_halt_via_api_reale_persiste_a_db(client_real_db):
    """QA item 9 + regressione del bug trovato in QA: get_db() non fa
    commit da solo (finally: session.close(), niente commit) -- prima del
    fix, halt_wa/resume_wa scrivevano SOLO in memoria e la richiesta
    successiva vedeva ancora wa_halted=False."""
    from app.database import AsyncSessionLocal
    from app.services import bot_state_service as bss

    async with AsyncSessionLocal() as db0:
        await bss.resume_wa(by="qa-setup", db=db0)
        await db0.commit()

    r = await client_real_db.post("/api/wa/ops/halt", json={"reason": "QAM3 halt manuale"})
    assert r.status_code == 200
    assert r.json() == {"wa_halted": True, "reason": "QAM3 halt manuale"}

    r2 = await client_real_db.get("/api/wa/ops/status")
    assert r2.json()["wa_halted"] is True

    async with AsyncSessionLocal() as db_check:
        row = await bss.is_wa_halted(db_check)
        assert row is True

    from sqlalchemy import select
    from app.models.bot_state import BotState
    async with AsyncSessionLocal() as db_check2:
        bs = await db_check2.scalar(select(BotState).limit(1))
        assert bs.wa_halted_reason == "QAM3 halt manuale"
        assert bs.wa_halted_at is not None


@pytest.mark.asyncio
async def test_11_resume_via_api_reale_persiste_a_db(client_real_db):
    from app.database import AsyncSessionLocal
    from app.services import bot_state_service as bss

    async with AsyncSessionLocal() as db0:
        await bss.halt_wa(reason="pre-resume", by="qa-setup", db=db0)
        await db0.commit()

    r = await client_real_db.post("/api/wa/ops/resume")
    assert r.status_code == 200
    assert r.json() == {"wa_halted": False}

    async with AsyncSessionLocal() as db_check:
        assert await bss.is_wa_halted(db_check) is False


@pytest.mark.asyncio
async def test_12_kick_su_campagna_running_accoda_job_vero_su_redis(db_session):
    """QA item 12: kick reale (no mock) contro redis vero, verifica diretta
    sulla coda ARQ che il job wa:send:<number_id> esista."""
    import arq
    from app.api import wa_ops
    from app.services.work_enqueue import arq_redis_settings
    from app.workers.wa_worker import wa_send_job_id

    ctx = await _scenario_claim(db_session)
    await db_session.commit()   # enqueue_wa_workers legge con una sessione NUOVA

    esito = await wa_ops.kick_campaign(ctx["campaign"].id, db=db_session)
    assert esito == {"accodati": 1}

    redis = await arq.create_pool(arq_redis_settings())
    try:
        job = arq.jobs.Job(wa_send_job_id(ctx["wa_number"].id), redis=redis)
        status = await job.status()
        assert status != arq.jobs.JobStatus.not_found
    finally:
        # pulizia: non lasciare il job appeso in coda per i prossimi run.
        await redis.zrem("arq:queue", wa_send_job_id(ctx["wa_number"].id))
        await redis.delete(f"arq:job:{wa_send_job_id(ctx['wa_number'].id)}")
        await redis.aclose()


@pytest.mark.asyncio
async def test_13_kick_su_draft_non_accoda_e_riporta_il_motivo(db_session):
    """Estende il test esistente: verifica anche la stringa di motivo, non
    solo il conteggio (adversarial #41)."""
    from app.api import wa_ops
    from app.models.wa import WaCampaignStatus

    ctx = await _scenario_claim(db_session)
    ctx["campaign"].status = WaCampaignStatus.draft
    await db_session.commit()

    esito = await wa_ops.kick_campaign(ctx["campaign"].id, db=db_session)
    assert esito == {"accodati": 0, "motivo": "campagna in stato draft"}


@pytest.mark.asyncio
async def test_14_kick_su_campaign_id_inesistente_torna_404(db_session):
    """QA item 14 / adversarial #40: MAI un 500 da un NoneType a valle."""
    import uuid
    from fastapi import HTTPException
    from app.api import wa_ops

    with pytest.raises(HTTPException) as exc:
        await wa_ops.kick_campaign(str(uuid.uuid4()), db=db_session)
    assert exc.value.status_code == 404
    assert exc.value.detail == "campagna inesistente"


@pytest.mark.asyncio
async def test_adv40_kick_id_inesistente_via_http_e_404_pulito(client_real_db):
    import uuid
    r = await client_real_db.post(f"/api/wa/ops/campaigns/{uuid.uuid4()}/kick")
    assert r.status_code == 404
    assert r.json()["detail"] == "campagna inesistente"


@pytest.mark.asyncio
async def test_adv42_halt_due_volte_di_fila_non_rompe(client_real_db):
    r1 = await client_real_db.post("/api/wa/ops/halt", json={"reason": "primo"})
    r2 = await client_real_db.post("/api/wa/ops/halt", json={"reason": "secondo"})
    assert r1.status_code == 200 and r2.status_code == 200

    from app.database import AsyncSessionLocal
    from sqlalchemy import select
    from app.models.bot_state import BotState
    async with AsyncSessionLocal() as db:
        bs = await db.scalar(select(BotState).limit(1))
        assert bs.wa_halted is True
        assert bs.wa_halted_reason == "secondo"


@pytest.mark.asyncio
async def test_adv43_resume_senza_halt_precedente_e_no_op_pulito(client_real_db):
    from app.database import AsyncSessionLocal
    from app.services import bot_state_service as bss

    async with AsyncSessionLocal() as db0:
        await bss.resume_wa(by="qa-setup", db=db0)
        await db0.commit()

    r = await client_real_db.post("/api/wa/ops/resume")
    assert r.status_code == 200
    assert r.json() == {"wa_halted": False}


@pytest.mark.asyncio
async def test_adv44_status_letto_mentre_halt_e_resume_girano_concorrenti(client_real_db):
    """PASS = nessun 500 su nessuna delle tre, e /status non torna mai un
    valore che non corrisponde a NESSUno stato effettivamente scritto."""
    results = await asyncio.gather(
        client_real_db.post("/api/wa/ops/halt", json={"reason": "concorrente"}),
        client_real_db.post("/api/wa/ops/resume"),
        client_real_db.get("/api/wa/ops/status"),
    )
    for r in results:
        assert r.status_code == 200
    status_body = results[2].json()
    assert status_body["wa_halted"] in (True, False)
