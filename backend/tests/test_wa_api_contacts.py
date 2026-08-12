from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.database import get_db
from app.main import app
from app.models.user import User
from app.utils.auth_deps import get_current_user
from tests.factories_wa import (make_campaign, make_campaign_contact, make_contact,
                                make_number, make_tenant)


def _admin_utente() -> User:
    return User(id="00000000-0000-0000-0000-000000000009", email="admin-wa@test.local",
               password_hash="x", role="admin", is_active=True,
               created_at=datetime.utcnow())


@pytest_asyncio.fixture
async def client(db_session):
    """Override reali di get_db (stessa sessione del test, cosi' le scritture
    dell'endpoint sono visibili alle assert dopo la chiamata HTTP) e di
    get_current_user (bypassa l'auth): senza, ogni richiesta risolve in 401 e
    il test passa per costruzione senza esercitare la logica vera -- gap
    trovato in review dedicata sulla prima versione di questo file."""
    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _admin_utente
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_ingest_risponde_col_report(db_session, client):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    await db_session.commit()

    r = await client.post(
        "/api/wa/contacts/ingest",
        data={"campaign_id": campaign.id},
        files={"file": ("lista.csv", b"numero,nome\n+393331112223,Marco\n", "text/csv")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["creati"] == 1
    assert body["scarti"] == []


@pytest.mark.asyncio
async def test_file_non_csv_rifiutato_con_422_non_500(db_session, client):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    await db_session.commit()
    r = await client.post(
        "/api/wa/contacts/ingest",
        data={"campaign_id": campaign.id},
        files={"file": ("foto.png", b"\x89PNG\r\n\x1a\n", "image/png")},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_ingest_su_campagna_running_e_rifiutato(db_session, client):
    """Macchina a stati: la lista si carica in draft. Aggiungere contatti a
    una campagna che sta girando cambia il denominatore dei KPI sotto i
    piedi al worker."""
    from app.models.wa import WaCampaignStatus
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number,
                                      status=WaCampaignStatus.running)
    await db_session.commit()
    r = await client.post(
        "/api/wa/contacts/ingest",
        data={"campaign_id": campaign.id},
        files={"file": ("l.csv", b"numero\n+393331112223\n", "text/csv")},
    )
    assert r.status_code == 409, r.text


@pytest.mark.asyncio
async def test_lista_contatti_maschera_sempre_il_numero(db_session, client):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    contact = await make_contact(db_session, tenant, e164="+393421460077")
    await make_campaign_contact(db_session, campaign, contact)
    await db_session.commit()

    r = await client.get(f"/api/wa/contacts?campaign_id={campaign.id}")
    assert r.status_code == 200, r.text
    testo = r.text
    assert "3421460077" not in testo
    assert "•" in testo


@pytest.mark.asyncio
async def test_lista_contatti_in_lavorazione_coerente_con_delete(db_session, client):
    """Trovato in review: GET marcava 'in_lavorazione' solo guardando
    locked_by, ignorando la staleness -- un lock di 5 ore fa risultava
    'in lavorazione' anche se DELETE lo avrebbe gia' accettato come libero.
    Le due viste devono concordare sulla stessa soglia (wa_lock_timeout_min)."""
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    contact = await make_contact(db_session, tenant)
    cc = await make_campaign_contact(db_session, campaign, contact)
    cc.locked_by = "worker-vivo"
    cc.locked_at = datetime.utcnow() - timedelta(hours=5)   # ben oltre il timeout
    await db_session.commit()

    r = await client.get(f"/api/wa/contacts?campaign_id={campaign.id}")
    assert r.status_code == 200, r.text
    riga = r.json()["contatti"][0]
    assert riga["in_lavorazione"] is False

    r2 = await client.delete(f"/api/wa/contacts/{cc.id}")
    assert r2.status_code == 200, r2.text


@pytest.mark.asyncio
async def test_rimozione_contatto_sotto_lock_fresco_rifiutata(db_session, client):
    """Invariante I1: M2 LEGGE i campi di lock e non li scrive. Una riga
    sotto lock e' in mano al worker di M3 in questo momento."""
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    contact = await make_contact(db_session, tenant)
    cc = await make_campaign_contact(db_session, campaign, contact)
    cc.locked_by = "worker-vivo"
    cc.locked_at = datetime.utcnow()
    await db_session.commit()

    r = await client.delete(f"/api/wa/contacts/{cc.id}")
    assert r.status_code == 409, r.text
    await db_session.refresh(cc)
    assert cc.locked_by == "worker-vivo"        # I1: mai toccato

    cc.locked_at = datetime.utcnow() - timedelta(minutes=45)   # lock stale
    await db_session.commit()
    r2 = await client.delete(f"/api/wa/contacts/{cc.id}")
    assert r2.status_code == 200, r2.text
    assert r2.json() == {"rimosso": True}


@pytest.mark.asyncio
async def test_rimozione_decrementa_total_contacts(db_session, client):
    """Trovato in review dedicata (Task 12): total_contacts non veniva mai
    aggiornato alla rimozione, restava per sempre disallineato dal
    conteggio reale -- visibile nella UI del dettaglio campagna."""
    from app.models.wa import WaCampaign
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    contact_a = await make_contact(db_session, tenant, e164="+393331112223")
    contact_b = await make_contact(db_session, tenant, e164="+393334445556")
    cc_a = await make_campaign_contact(db_session, campaign, contact_a)
    await make_campaign_contact(db_session, campaign, contact_b)
    campaign.total_contacts = 2
    await db_session.commit()

    r = await client.delete(f"/api/wa/contacts/{cc_a.id}")
    assert r.status_code == 200, r.text
    await db_session.refresh(campaign)
    assert campaign.total_contacts == 1


@pytest.mark.asyncio
async def test_rimozione_cancella_il_contatto_orfano(db_session, client):
    """Trovato in Fase 4 QA (adversarial #46): rimuovere un contatto dalla
    SUA UNICA campagna lasciava il WaContact (numero cifrato, hmac) a DB
    per sempre -- anagrafica orfana, contro la minimizzazione dichiarata
    (Q23: 'l'ingest crea SOLO i contatti della campagna')."""
    from sqlalchemy import select
    from app.models.wa import WaContact
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    contact = await make_contact(db_session, tenant, e164="+393331112223")
    cc = await make_campaign_contact(db_session, campaign, contact)
    await db_session.commit()
    contact_id = contact.id

    r = await client.delete(f"/api/wa/contacts/{cc.id}")
    assert r.status_code == 200, r.text
    ancora_a_db = await db_session.scalar(select(WaContact).where(WaContact.id == contact_id))
    assert ancora_a_db is None


@pytest.mark.asyncio
async def test_rimozione_non_cancella_un_contatto_usato_da_altra_campagna(db_session, client):
    """Lo scopo dichiarato di WaContact e' il dedup CROSS-campagna: un
    contatto ancora referenziato da un'altra campagna non deve sparire."""
    from sqlalchemy import select
    from app.models.wa import WaContact
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign_a, _ = await make_campaign(db_session, tenant, number, name="A")
    campaign_b, _ = await make_campaign(db_session, tenant, number, name="B")
    contact = await make_contact(db_session, tenant, e164="+393331112223")
    cc_a = await make_campaign_contact(db_session, campaign_a, contact)
    await make_campaign_contact(db_session, campaign_b, contact)
    await db_session.commit()
    contact_id = contact.id

    r = await client.delete(f"/api/wa/contacts/{cc_a.id}")
    assert r.status_code == 200, r.text
    ancora_a_db = await db_session.scalar(select(WaContact).where(WaContact.id == contact_id))
    assert ancora_a_db is not None      # ancora referenziato da campaign_b


@pytest.mark.asyncio
async def test_enroll_verso_campagna_non_draft_409(db_session, client):
    """Fase B, Task 3/4: stesso guard 409 di /ingest, qui via
    CampagnaNonModificabile tradotta dal guscio HTTP."""
    from app.models.wa import WaCampaignStatus
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number,
                                      status=WaCampaignStatus.running)
    contact = await make_contact(db_session, tenant)
    await db_session.commit()

    r = await client.post("/api/wa/contacts/enroll",
                          json={"campaign_id": campaign.id, "contact_ids": [contact.id]})
    assert r.status_code == 409, r.text


@pytest.mark.asyncio
async def test_enroll_con_contatti_validi_200_e_crea_wacampaigncontact(db_session, client):
    from sqlalchemy import select
    from app.models.wa import WaCampaignContact
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    contact_a = await make_contact(db_session, tenant, e164="+393331112223")
    contact_b = await make_contact(db_session, tenant, e164="+393334445556")
    await db_session.commit()

    r = await client.post(
        "/api/wa/contacts/enroll",
        json={"campaign_id": campaign.id, "contact_ids": [contact_a.id, contact_b.id]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["arruolati"] == 2
    assert body["gia_presenti"] == 0
    assert body["gia_dnc"] == 0
    assert body["scarti"] == []

    righe = (await db_session.execute(
        select(WaCampaignContact).where(WaCampaignContact.campaign_id == campaign.id)
    )).scalars().all()
    assert len(righe) == 2
    await db_session.refresh(campaign)
    assert campaign.total_contacts == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stato_terminale", ["replied", "skipped"])
async def test_rimozione_rifiutata_su_ogni_stato_terminale(db_session, client, stato_terminale):
    """Trovato in whole-branch review: la guardia controllava solo
    opted_out/completed, ma WaContactStatus ha QUATTRO stati terminali
    (replied, completed, opted_out, skipped) -- invisibile finche' M2 da
    solo non crea mai righe in quegli stati (li scrive M3), ma un contatto
    che ha risposto o uno scarto diagnostico sarebbe stato rimovibile."""
    from app.models.wa import WaContactStatus
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    contact = await make_contact(db_session, tenant)
    cc = await make_campaign_contact(db_session, campaign, contact,
                                     status=WaContactStatus(stato_terminale))
    await db_session.commit()

    r = await client.delete(f"/api/wa/contacts/{cc.id}")
    assert r.status_code == 409, r.text
