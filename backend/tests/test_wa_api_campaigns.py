"""Test HTTP del router wa_campaigns (Task 6). Dependency_overrides REALI su
get_db/get_current_user (stesso pattern di test_wa_api_contacts.py, gia'
corretto in questo cantiere): senza, ogni richiesta risolve in 401 e il test
passerebbe per costruzione senza esercitare la logica vera.
"""
from datetime import datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import get_db
from app.main import app
from app.models.user import User
from app.models.wa import WaCampaign, WaCampaignStatus, WaCampaignType
from app.utils.auth_deps import get_current_user
from tests.factories_wa import (make_campaign, make_campaign_contact, make_contact,
                                make_number, make_tenant)


def _admin_utente() -> User:
    return User(id="00000000-0000-0000-0000-00000000000a", email="admin-wa-camp@test.local",
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


@pytest.mark.asyncio
async def test_post_crea_followup_ha_optout_false_a_db(db_session, client):
    """Prova esplicita anche a livello HTTP, non solo di funzione: la riga a
    DB deve avere optout_enabled=False per una followup creata via API."""
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await db_session.commit()

    r = await client.post("/api/wa/campaigns", json={
        "tenant_id": tenant.id, "wa_number_id": number.id, "name": "follow-http",
        "campaign_type": "followup", "template_a": "Ciao {nome}.",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["optout_enabled"] is False

    riga = await db_session.scalar(select(WaCampaign).where(WaCampaign.id == body["id"]))
    assert riga.optout_enabled is False


@pytest.mark.asyncio
async def test_post_marketing_senza_cta_risponde_422(db_session, client):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await db_session.commit()

    r = await client.post("/api/wa/campaigns", json={
        "tenant_id": tenant.id, "wa_number_id": number.id, "name": "promo-http",
        "campaign_type": "marketing", "template_a": "Ciao {nome}.",
    })
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_post_template_con_placeholder_ignoto_risponde_422(db_session, client):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await db_session.commit()

    r = await client.post("/api/wa/campaigns", json={
        "tenant_id": tenant.id, "wa_number_id": number.id, "name": "x",
        "campaign_type": "followup", "template_a": "Ciao {nome}, ordine {ultimo_ordine}.",
    })
    assert r.status_code == 422, r.text
    assert "ultimo_ordine" in r.text


@pytest.mark.asyncio
async def test_post_su_numero_di_un_altro_tenant_risponde_422(db_session, client):
    tenant_a = await make_tenant(db_session, name="A-http")
    tenant_b = await make_tenant(db_session, name="B-http")
    number_b = await make_number(db_session, tenant_b)
    await db_session.commit()

    r = await client.post("/api/wa/campaigns", json={
        "tenant_id": tenant_a.id, "wa_number_id": number_b.id, "name": "x",
        "campaign_type": "followup", "template_a": "Ciao.",
    })
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_get_lista_e_dettaglio(db_session, client):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    await db_session.commit()

    r = await client.get(f"/api/wa/campaigns?tenant_id={tenant.id}")
    assert r.status_code == 200, r.text
    assert any(c["id"] == campaign.id for c in r.json()["campagne"])

    r2 = await client.get(f"/api/wa/campaigns/{campaign.id}")
    assert r2.status_code == 200, r2.text
    assert r2.json()["step_0"]["template_a"] == "Ciao {nome}, promo attiva."


@pytest.mark.asyncio
async def test_patch_su_campagna_running_rifiutato(db_session, client):
    """Macchina a stati: le modifiche di contenuto/CTA si fanno solo in
    draft. A campagna avviata si passa da pausa (Task 7)."""
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number,
                                      status=WaCampaignStatus.running)
    await db_session.commit()

    r = await client.patch(f"/api/wa/campaigns/{campaign.id}", json={"name": "nuovo nome"})
    assert r.status_code == 409, r.text


@pytest.mark.asyncio
async def test_patch_in_draft_applica_solo_i_campi_ammessi(db_session, client):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number,
                                      tipo=WaCampaignType.followup)
    await db_session.commit()

    r = await client.patch(f"/api/wa/campaigns/{campaign.id}", json={
        "name": "nome nuovo", "daily_limit": 50,
        "status": "running",       # deve essere ignorato: non e' in CAMPI_MODIFICABILI
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "nome nuovo"
    assert body["daily_limit"] == 50
    assert body["status"] == "draft"       # non scavalcato dal body


@pytest.mark.asyncio
async def test_put_step0_con_placeholder_coperto_dal_csv_si_salva(db_session, client):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number,
                                      tipo=WaCampaignType.followup)
    contact = await make_contact(db_session, tenant, attributes={"ultimo_ordine": "10/01/2026"})
    await make_campaign_contact(db_session, campaign, contact)
    await db_session.commit()

    r = await client.put(f"/api/wa/campaigns/{campaign.id}/steps/0", json={
        "template_a": "Ciao {nome}, ordine {ultimo_ordine}.",
    })
    assert r.status_code == 200, r.text
    assert r.json()["template_a"] == "Ciao {nome}, ordine {ultimo_ordine}."


@pytest.mark.asyncio
async def test_put_step0_con_placeholder_non_coperto_risponde_422(db_session, client):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number,
                                      tipo=WaCampaignType.followup)
    contact = await make_contact(db_session, tenant, attributes={"citta": "Roma"})
    await make_campaign_contact(db_session, campaign, contact)
    await db_session.commit()

    r = await client.put(f"/api/wa/campaigns/{campaign.id}/steps/0", json={
        "template_a": "Ciao {nome}, ordine {ultimo_ordine}.",
    })
    assert r.status_code == 422, r.text
    assert "ultimo_ordine" in r.text
