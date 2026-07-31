"""Test HTTP degli endpoint di ciclo vita (Task 7): start/pause/resume/stop.
Dependency_overrides REALI su get_db/get_current_user (stesso pattern di
test_wa_api_campaigns.py, gia' corretto in questo cantiere): senza, ogni
richiesta risolve in 401 e il test passerebbe per costruzione senza
esercitare la logica vera.

Motivazione della prova esplicita di "1 campagna running per numero" anche
qui: nel Task 6 il bug (optout_enabled bypassabile) stava proprio nel
percorso HTTP che i test di servizio non coprivano. La stessa regola va
quindi provata SIA a livello di servizio (test_wa_campaign_lifecycle.py)
SIA qui, passando dal router vero.
"""
from datetime import datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import get_db
from app.main import app
from app.models.user import User
from app.models.wa import WaCampaign, WaCampaignStatus
from app.utils.auth_deps import get_current_user
from tests.factories_wa import (make_campaign, make_campaign_contact, make_contact,
                                make_number, make_tenant)


def _admin_utente() -> User:
    return User(id="00000000-0000-0000-0000-00000000000b", email="admin-wa-lifecycle@test.local",
               password_hash="x", role="admin", is_active=True,
               created_at=datetime.utcnow())


@pytest_asyncio.fixture
async def client(db_session):
    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _admin_utente
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _pronta(db):
    tenant = await make_tenant(db)
    number = await make_number(db, tenant)
    campaign, _ = await make_campaign(db, tenant, number)
    contact = await make_contact(db, tenant)
    await make_campaign_contact(db, campaign, contact)
    await db.commit()
    return tenant, number, campaign


@pytest.mark.asyncio
async def test_start_via_http_porta_a_running(db_session, client):
    _, _, campaign = await _pronta(db_session)
    r = await client.post(f"/api/wa/campaigns/{campaign.id}/start")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "running"

    riga = await db_session.scalar(select(WaCampaign).where(WaCampaign.id == campaign.id))
    assert riga.status == WaCampaignStatus.running


@pytest.mark.asyncio
async def test_seconda_campagna_running_sullo_stesso_numero_rifiutata_via_http(db_session, client):
    """Prova esplicita a livello HTTP, non solo di servizio (vedi nota in
    testa al file): un client che chiama /start su una seconda campagna
    dello stesso numero mentre la prima e' running deve ricevere 422, non
    un 200 che lascerebbe due campagne a correre sullo stesso pacing."""
    tenant, number, campaign_a = await _pronta(db_session)
    r_a = await client.post(f"/api/wa/campaigns/{campaign_a.id}/start")
    assert r_a.status_code == 200, r_a.text

    campaign_b, _ = await make_campaign(db_session, tenant, number, name="seconda-http")
    contact_b = await make_contact(db_session, tenant)
    await make_campaign_contact(db_session, campaign_b, contact_b)
    await db_session.commit()

    r_b = await client.post(f"/api/wa/campaigns/{campaign_b.id}/start")
    assert r_b.status_code == 422, r_b.text

    riga_b = await db_session.scalar(select(WaCampaign).where(WaCampaign.id == campaign_b.id))
    assert riga_b.status == WaCampaignStatus.draft


@pytest.mark.asyncio
async def test_pause_poi_resume_via_http(db_session, client):
    _, _, campaign = await _pronta(db_session)
    r1 = await client.post(f"/api/wa/campaigns/{campaign.id}/start")
    assert r1.status_code == 200, r1.text

    r2 = await client.post(f"/api/wa/campaigns/{campaign.id}/pause")
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "paused"

    r3 = await client.post(f"/api/wa/campaigns/{campaign.id}/resume")
    assert r3.status_code == 200, r3.text
    assert r3.json()["status"] == "running"


@pytest.mark.asyncio
async def test_resume_bloccato_se_nel_frattempo_un_altra_campagna_e_partita_sul_numero(
        db_session, client):
    """Caso limite specifico del pacing per-job: la campagna A e' in
    pausa, nel frattempo la campagna B parte sullo stesso numero. Riprendere
    A darebbe due campagne running sullo stesso numero: deve restare
    bloccato, non solo lo start iniziale."""
    tenant, number, campaign_a = await _pronta(db_session)
    await client.post(f"/api/wa/campaigns/{campaign_a.id}/start")
    await client.post(f"/api/wa/campaigns/{campaign_a.id}/pause")

    campaign_b, _ = await make_campaign(db_session, tenant, number, name="b-durante-pausa-a")
    contact_b = await make_contact(db_session, tenant)
    await make_campaign_contact(db_session, campaign_b, contact_b)
    await db_session.commit()
    r_b = await client.post(f"/api/wa/campaigns/{campaign_b.id}/start")
    assert r_b.status_code == 200, r_b.text

    r_resume_a = await client.post(f"/api/wa/campaigns/{campaign_a.id}/resume")
    assert r_resume_a.status_code == 422, r_resume_a.text


@pytest.mark.asyncio
async def test_stop_via_http(db_session, client):
    _, _, campaign = await _pronta(db_session)
    await client.post(f"/api/wa/campaigns/{campaign.id}/start")
    r = await client.post(f"/api/wa/campaigns/{campaign.id}/stop")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "stopped"


@pytest.mark.asyncio
async def test_start_senza_contatti_risponde_422_via_http(db_session, client):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    await db_session.commit()

    r = await client.post(f"/api/wa/campaigns/{campaign.id}/start")
    assert r.status_code == 422, r.text
