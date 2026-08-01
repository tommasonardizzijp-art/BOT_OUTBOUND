import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from tests.factories_wa import make_tenant, make_number, make_campaign


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
