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
    # Questa fixture committa scritture VERE a DB (e' il punto: get_db non
    # e' overridden). Senza reset, un test che halta lascia wa_halted=True
    # per il prossimo test che usa questa fixture -- oggi mascherato perche'
    # test_wa_worker.py monkeypatcha is_wa_halted ovunque, ma e' una
    # protezione accidentale di un altro file, non una garanzia.
    from app.database import AsyncSessionLocal
    from app.services import bot_state_service as bss
    async with AsyncSessionLocal() as db:
        await bss.resume_wa(by="qa-teardown", db=db)
        await db.commit()


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
async def test_g3_status_espone_motivo_stop_quando_il_canale_e_fermo(db_session):
    """G3: la striscia di stato frontend deve poter mostrare PERCHE' il
    canale si e' fermato prima di lasciar premere 'Riprendi'. Il motivo e'
    gia' salvato a DB (bot_state.wa_halted_reason, fix B1 di PR #52): qui
    va solo esposto in /wa/ops/status, che oggi non lo espone."""
    from app.api import wa_ops
    from app.services import bot_state_service as bss

    await bss.halt_wa(reason="breaker: campagna X al 30% opt-out", by="test", db=db_session)
    await db_session.commit()

    body = await wa_ops.wa_ops_status(db=db_session)
    assert body["wa_halted"] is True
    assert body["motivo_stop"] == "breaker: campagna X al 30% opt-out"


@pytest.mark.asyncio
async def test_g3_status_motivo_stop_none_quando_il_canale_non_e_fermo(db_session):
    from app.api import wa_ops
    from app.services import bot_state_service as bss

    await bss.resume_wa(by="test", db=db_session)
    await db_session.commit()

    body = await wa_ops.wa_ops_status(db=db_session)
    assert body["wa_halted"] is False
    assert body["motivo_stop"] is None


async def _ritira_numeri_attivi_preesistenti(db_session) -> None:
    """Altri test di questo file (es. test_8) committano DAVVERO a DB numeri
    WaNumber 'active' -- db_session fa rollback in teardown, ma un commit
    esplicito a meta' test sopravvive lo stesso (nuova transazione dopo il
    commit, il rollback di teardown non tocca quella gia' chiusa). I test
    'cap_effettivo' qui sotto verificano un MINIMO/None fra i numeri attivi:
    senza questa pulizia vedrebbero anche i residui di test precedenti nello
    stesso run e il risultato dipenderebbe dall'ordine di esecuzione."""
    from sqlalchemy import update
    from app.models.wa import WaNumber, WaNumberStatus

    await db_session.execute(update(WaNumber).values(status=WaNumberStatus.retired))
    await db_session.commit()


@pytest.mark.asyncio
async def test_g4_status_cap_effettivo_e_il_minimo_tra_i_numeri_attivi(db_session, monkeypatch):
    """G4: la striscia deve mostrare il tetto REALE del canale, non solo se
    e' fermo o no -- e' l'unica protezione rimasta con la rampa di warmup
    disattivata (warmup_day=0). Semantica scelta per piu' numeri attivi:
    il MINIMO dei loro cap effettivi (il collo di bottiglia vero)."""
    from app.config import settings
    from app.api import wa_ops

    await _ritira_numeri_attivi_preesistenti(db_session)
    monkeypatch.setattr(settings, "wa_warmup_steps", "20,20,30,40,60,80,100")

    tenant = await make_tenant(db_session)
    # Un numero fuori warmup (warmup_day=0): il tetto e' il suo daily_cap "nudo".
    numero_a = await make_number(db_session, tenant, label="A")
    numero_a.daily_cap = 50
    numero_a.warmup_day = 0
    # Un secondo numero IN warmup: il suo tetto e' il gradino, piu' basso.
    numero_b = await make_number(db_session, tenant, label="B")
    numero_b.daily_cap = 200
    numero_b.warmup_day = 3   # 3o gradino della lista sopra = 30
    await db_session.commit()

    body = await wa_ops.wa_ops_status(db=db_session)
    assert body["numeri_attivi"] == 2
    assert body["cap_effettivo"] == 30   # il minimo, non il primo ne' la media


@pytest.mark.asyncio
async def test_g4_status_cap_effettivo_none_senza_numeri_attivi(db_session):
    """Nessun numero attivo -- nessun tetto da mostrare, non uno zero che
    si potrebbe leggere come 'canale bloccato a 0 invii/giorno'."""
    from app.api import wa_ops

    await _ritira_numeri_attivi_preesistenti(db_session)

    body = await wa_ops.wa_ops_status(db=db_session)
    assert body["cap_effettivo"] is None
    assert body["numeri_attivi"] == 0


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


@pytest_asyncio.fixture
async def _redis_o_skip():
    """Redis reale e' un requisito duro per questo test (QA item 12): se
    non e' raggiungibile (es. CI senza servizio Redis), skip esplicito con
    motivo chiaro invece di un rosso che sembra una regressione. Duplicata
    in test_wa_worker.py (stesso motivo di _scenario_claim: conftest.py e'
    congelato dopo PR-0, contratto §8.1)."""
    import arq
    from app.services.work_enqueue import arq_redis_settings
    try:
        pool = await arq.create_pool(arq_redis_settings())
        await pool.ping()
        await pool.aclose()
    except Exception as exc:
        pytest.skip(f"Redis non raggiungibile, test saltato: {type(exc).__name__}: {exc}")


@pytest.mark.asyncio
async def test_12_kick_su_campagna_running_accoda_job_vero_su_redis(db_session, _redis_o_skip):
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
async def test_resume_campagna_paused_rimette_running_e_accoda(db_session, monkeypatch):
    """Caso principale: campagna paused (es. dal cron wa_session_healthcheck)
    torna running e il worker viene riaccodato."""
    from app.api import wa_ops
    from app.models.wa import WaCampaignStatus

    ctx = await _scenario_claim(db_session)
    ctx["campaign"].status = WaCampaignStatus.paused
    await db_session.commit()

    accodati = {"n": 0}

    async def _fake_enqueue(campaign_id):
        accodati["n"] += 1
        return 1
    monkeypatch.setattr(wa_ops, "enqueue_wa_workers", _fake_enqueue)

    esito = await wa_ops.resume_campaign(ctx["campaign"].id, db=db_session)
    assert esito == {"resumed": True, "accodati": 1}
    assert accodati["n"] == 1

    await db_session.refresh(ctx["campaign"])
    assert ctx["campaign"].status == WaCampaignStatus.running


@pytest.mark.asyncio
@pytest.mark.parametrize("stato", ["draft", "running", "completed", "stopped", "error"])
async def test_resume_campagna_non_paused_non_tocca_nulla(db_session, monkeypatch, stato):
    """Su qualunque stato diverso da paused, resume e' un no-op idempotente:
    non scrive lo stato e non accoda nulla."""
    from app.api import wa_ops
    from app.models.wa import WaCampaignStatus

    ctx = await _scenario_claim(db_session)
    stato_enum = WaCampaignStatus(stato)
    ctx["campaign"].status = stato_enum
    await db_session.commit()

    accodati = {"n": 0}

    async def _fake_enqueue(campaign_id):
        accodati["n"] += 1
        return 1
    monkeypatch.setattr(wa_ops, "enqueue_wa_workers", _fake_enqueue)

    esito = await wa_ops.resume_campaign(ctx["campaign"].id, db=db_session)
    assert esito == {"resumed": False,
                     "motivo": f"campagna in stato {stato}, non paused"}
    assert accodati["n"] == 0

    await db_session.refresh(ctx["campaign"])
    assert ctx["campaign"].status == stato_enum


@pytest.mark.asyncio
@pytest.mark.parametrize("stato_numero", ["qr_required", "cooldown", "suspended", "retired"])
async def test_resume_campagna_paused_con_numero_non_active_non_flippa(db_session, monkeypatch, stato_numero):
    """Review finale (Major): il caso reale di pausa e' quasi sempre
    sessione WhatsApp caduta -> numero non-active. Se il resume flippasse
    comunque la campagna a running, produrrebbe una campagna 'running'
    fantasma senza job attivo (il worker rifiuta l'invio su numero
    non-active e non ri-schedula). Deve rifiutarsi, non solo il worker a
    valle."""
    from app.api import wa_ops
    from app.models.wa import WaCampaignStatus, WaNumberStatus

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant, status=WaNumberStatus(stato_numero))
    campaign, _ = await make_campaign(db_session, tenant, numero,
                                      status=WaCampaignStatus.paused)
    await db_session.commit()

    accodati = {"n": 0}

    async def _fake_enqueue(campaign_id):
        accodati["n"] += 1
        return 1
    monkeypatch.setattr(wa_ops, "enqueue_wa_workers", _fake_enqueue)

    esito = await wa_ops.resume_campaign(campaign.id, db=db_session)
    assert esito == {"resumed": False, "motivo": f"numero in stato {stato_numero}, non active"}
    assert accodati["n"] == 0

    await db_session.refresh(campaign)
    assert campaign.status == WaCampaignStatus.paused


@pytest.mark.asyncio
async def test_resume_campagna_paused_enqueue_fallisce_non_perde_lo_stato(db_session, monkeypatch):
    """Review finale (Major): db.commit() dello stato avviene prima
    dell'enqueue fallibile su Redis. Se Redis e' giu' in quel momento,
    l'eccezione non deve risalire come 500: lo stato e' gia' scritto (non
    e' un problema di sicurezza, la campagna resta comunque 'ferma' finche'
    nessun job la muove), la risposta deve dirlo onestamente invece di
    fingere un errore totale."""
    from app.api import wa_ops
    from app.models.wa import WaCampaignStatus

    ctx = await _scenario_claim(db_session)
    ctx["campaign"].status = WaCampaignStatus.paused
    await db_session.commit()

    async def _fake_enqueue_boom(campaign_id):
        raise ConnectionError("redis giu'")
    monkeypatch.setattr(wa_ops, "enqueue_wa_workers", _fake_enqueue_boom)

    esito = await wa_ops.resume_campaign(ctx["campaign"].id, db=db_session)
    assert esito["resumed"] is True
    assert esito["accodati"] == 0
    assert "redis giu'" in esito["errore_accodamento"]

    await db_session.refresh(ctx["campaign"])
    assert ctx["campaign"].status == WaCampaignStatus.running


@pytest.mark.asyncio
async def test_resume_campaign_id_inesistente_torna_404(db_session):
    """Stesso pattern esatto di kick: mai un 500 da un NoneType a valle."""
    import uuid
    from fastapi import HTTPException
    from app.api import wa_ops

    with pytest.raises(HTTPException) as exc:
        await wa_ops.resume_campaign(str(uuid.uuid4()), db=db_session)
    assert exc.value.status_code == 404
    assert exc.value.detail == "campagna inesistente"


@pytest.mark.asyncio
async def test_resume_due_volte_di_fila_e_idempotente(db_session, monkeypatch):
    """Adversarial: due resume in sequenza sulla stessa campagna. Il primo
    la fa ripartire e accoda; il secondo la trova gia' running e si
    comporta come il ramo non-paused (resumed False, nessun secondo
    accodamento) -- niente doppio riavvio di un worker gia' vivo."""
    from app.api import wa_ops
    from app.models.wa import WaCampaignStatus

    ctx = await _scenario_claim(db_session)
    ctx["campaign"].status = WaCampaignStatus.paused
    await db_session.commit()

    accodati = {"n": 0}

    async def _fake_enqueue(campaign_id):
        accodati["n"] += 1
        return 1
    monkeypatch.setattr(wa_ops, "enqueue_wa_workers", _fake_enqueue)

    primo = await wa_ops.resume_campaign(ctx["campaign"].id, db=db_session)
    assert primo == {"resumed": True, "accodati": 1}
    assert accodati["n"] == 1

    secondo = await wa_ops.resume_campaign(ctx["campaign"].id, db=db_session)
    assert secondo == {"resumed": False, "motivo": "campagna in stato running, non paused"}
    assert accodati["n"] == 1   # non e' salito: nessun secondo accodamento


@pytest.mark.asyncio
async def test_adv_resume_id_inesistente_via_http_e_404_pulito(client_real_db):
    import uuid
    r = await client_real_db.post(f"/api/wa/ops/campaigns/{uuid.uuid4()}/resume")
    assert r.status_code == 404
    assert r.json()["detail"] == "campagna inesistente"


@pytest.mark.asyncio
async def test_adv_resume_via_http_reale_persiste_a_db(client_real_db, monkeypatch):
    """Roundtrip API vero (get_db non overridden, sessione reale come
    client_real_db): verifica che lo stato scritto sopravviva oltre la
    request, non solo nella sessione della response."""
    from app.database import AsyncSessionLocal
    from app.models.wa import WaCampaignStatus
    from app.api import wa_ops

    async def _fake_enqueue(campaign_id):
        return 1
    monkeypatch.setattr(wa_ops, "enqueue_wa_workers", _fake_enqueue)

    async with AsyncSessionLocal() as db:
        ctx = await _scenario_claim(db)
        ctx["campaign"].status = WaCampaignStatus.paused
        await db.commit()
        campaign_id = ctx["campaign"].id

    r = await client_real_db.post(f"/api/wa/ops/campaigns/{campaign_id}/resume")
    assert r.status_code == 200
    assert r.json() == {"resumed": True, "accodati": 1}

    async with AsyncSessionLocal() as db_check:
        from sqlalchemy import select
        from app.models.wa import WaCampaign
        camp = await db_check.scalar(select(WaCampaign).where(WaCampaign.id == campaign_id))
        assert camp.status == WaCampaignStatus.running


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
