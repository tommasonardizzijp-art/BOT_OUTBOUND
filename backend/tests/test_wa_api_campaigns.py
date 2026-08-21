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
async def test_post_skip_history_gate_default_false(db_session, client):
    """Default invariato quando il payload non lo passa affatto."""
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await db_session.commit()

    r = await client.post("/api/wa/campaigns", json={
        "tenant_id": tenant.id, "wa_number_id": number.id, "name": "senza-toggle",
        "campaign_type": "followup", "template_a": "Ciao {nome}.",
    })
    assert r.status_code == 200, r.text
    assert r.json()["skip_history_gate"] is False


@pytest.mark.asyncio
async def test_post_skip_history_gate_true_si_accetta_dal_payload(db_session, client):
    """21/08: a differenza di optout_enabled, questo campo si accetta dal
    payload di creazione -- e' il toggle richiesto da Tommaso per non dover
    piu' editare un CSV in .env a ogni campagna nuova con contatti mai
    contattati dal bot."""
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await db_session.commit()

    r = await client.post("/api/wa/campaigns", json={
        "tenant_id": tenant.id, "wa_number_id": number.id, "name": "con-toggle",
        "campaign_type": "followup", "template_a": "Ciao {nome}.",
        "skip_history_gate": True,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skip_history_gate"] is True

    riga = await db_session.scalar(select(WaCampaign).where(WaCampaign.id == body["id"]))
    assert riga.skip_history_gate is True


@pytest.mark.asyncio
async def test_patch_skip_history_gate_modificabile_in_draft(db_session, client):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number,
                                      tipo=WaCampaignType.followup)
    await db_session.commit()
    assert campaign.skip_history_gate is False

    r = await client.patch(f"/api/wa/campaigns/{campaign.id}", json={
        "skip_history_gate": True,
    })
    assert r.status_code == 200, r.text
    assert r.json()["skip_history_gate"] is True


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


# ---------------------------------------------------------------------------
# Modifica di una campagna gia' avviata: si passa dalla PAUSA.
#
# Il messaggio d'errore prometteva gia' questa strada ("metti in pausa una
# campagna avviata prima di modificarla") ma il controllo esigeva `draft`, e
# non esiste nessuna transizione paused -> draft: seguendo l'istruzione si
# finiva su un secondo 409. In pratica il testo di una campagna, una volta
# avviata, non era piu' correggibile -- trovato dal vivo il 16/08 con una
# campagna da 666 contatti gia' partita e una CTA di opt-out da sistemare.
#
# 'paused' e' lo stato giusto in cui permetterlo: il consumo e' fermo, quindi
# non c'e' una mini-sessione che sta leggendo il template mentre lo si
# riscrive.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_patch_in_pausa_e_permesso(db_session, client):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number,
                                      status=WaCampaignStatus.paused)
    await db_session.commit()

    r = await client.patch(f"/api/wa/campaigns/{campaign.id}",
                           json={"optout_cta": "_Scrivi STOP_"})
    assert r.status_code == 200, r.text
    assert r.json()["optout_cta"] == "_Scrivi STOP_"


@pytest.mark.asyncio
async def test_put_step0_in_pausa_e_permesso(db_session, client):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number,
                                      status=WaCampaignStatus.paused)
    await db_session.commit()

    r = await client.put(f"/api/wa/campaigns/{campaign.id}/steps/0",
                         json={"template_a": "Testo corretto in pausa."})
    assert r.status_code == 200, r.text
    assert r.json()["template_a"] == "Testo corretto in pausa."


@pytest.mark.asyncio
async def test_su_una_campagna_in_corso_si_rifiuta_ancora(db_session, client):
    """Il consumo e' vivo: riscrivere il template sotto le mani della
    mini-sessione d'invio e' esattamente cio' che non deve succedere."""
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number,
                                      status=WaCampaignStatus.running)
    await db_session.commit()

    r = await client.patch(f"/api/wa/campaigns/{campaign.id}", json={"name": "x"})
    assert r.status_code == 409
    r = await client.put(f"/api/wa/campaigns/{campaign.id}/steps/0",
                         json={"template_a": "x"})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_su_una_campagna_fermata_si_rifiuta(db_session, client):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number,
                                      status=WaCampaignStatus.stopped)
    await db_session.commit()

    r = await client.patch(f"/api/wa/campaigns/{campaign.id}", json={"name": "x"})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_il_rifiuto_dice_una_strada_che_esiste(db_session, client):
    """Il vecchio testo mandava a mettere in pausa, ma poi la pausa veniva
    rifiutata lo stesso: un'istruzione che non porta da nessuna parte e'
    peggio di nessuna istruzione."""
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number,
                                      status=WaCampaignStatus.running)
    await db_session.commit()

    r = await client.patch(f"/api/wa/campaigns/{campaign.id}", json={"name": "x"})
    dettaglio = r.json()["detail"].lower()
    assert "pausa" in dettaglio
    assert "bozza" in dettaglio or "in pausa" in dettaglio


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
