"""Test di backend/app/api/wa_discover.py (Fase B, Task 4).

Stile di test_wa_api_contacts.py: client fixture con override di get_db (stessa
sessione del test) e get_current_user (bypassa l'auth) -- senza, ogni
richiesta risolverebbe in 401 e il test passerebbe per costruzione senza
esercitare la logica vera.

Un caso per comportamento distinto, non per variazione cosmetica (409/422 su
varianti dello stesso guard non hanno un test a parte se un altro test gia' lo
copre a livello di servizio, coperto in test_wa_promote_promozione.py /
test_wa_promote_arruolamento.py -- qui si verifica SOLO il guscio HTTP: 404 di
number_id, filtri GET, visibilita' dei gruppi, la barriera IDOR via
number_id/tenant_id.
"""
from datetime import datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import get_db
from app.main import app
from app.models.user import User
from app.models.wa import WaContact, WaDiscoveredChat
from app.utils.auth_deps import get_current_user
from tests.factories_wa import make_discovered_chat, make_number, make_tenant


def _admin_utente() -> User:
    return User(id="00000000-0000-0000-0000-00000000000a", email="admin-wa-discover@test.local",
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
async def test_get_con_number_id_inesistente_404(db_session, client):
    r = await client.get("/api/wa/discovered-chats?number_id=00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_get_filtra_per_tipo_chat_ha_numero_status(db_session, client):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    individuale = await make_discovered_chat(db_session, tenant, number,
                                             tipo_chat="individuale", chat_title="Mario")
    gruppo = await make_discovered_chat(db_session, tenant, number, tipo_chat="gruppo",
                                        e164=None, chat_title="Famiglia Rossi")
    senza_numero = await make_discovered_chat(db_session, tenant, number,
                                              tipo_chat="individuale", e164=None,
                                              chat_title="Senza Numero")
    gia_promosso = await make_discovered_chat(db_session, tenant, number,
                                              tipo_chat="individuale", status="promosso",
                                              chat_title="Gia Promosso")
    await db_session.commit()

    # default status='nuovo': il gia_promosso resta fuori
    r = await client.get(f"/api/wa/discovered-chats?number_id={number.id}")
    assert r.status_code == 200, r.text
    ids = {c["id"] for c in r.json()["chat"]}
    assert ids == {individuale.id, gruppo.id, senza_numero.id}

    r = await client.get(f"/api/wa/discovered-chats?number_id={number.id}&tipo_chat=gruppo")
    assert r.status_code == 200, r.text
    ids = {c["id"] for c in r.json()["chat"]}
    assert ids == {gruppo.id}

    r = await client.get(f"/api/wa/discovered-chats?number_id={number.id}&ha_numero=true")
    assert r.status_code == 200, r.text
    ids = {c["id"] for c in r.json()["chat"]}
    assert ids == {individuale.id}

    r = await client.get(f"/api/wa/discovered-chats?number_id={number.id}&ha_numero=false")
    assert r.status_code == 200, r.text
    ids = {c["id"] for c in r.json()["chat"]}
    assert ids == {gruppo.id, senza_numero.id}

    r = await client.get(f"/api/wa/discovered-chats?number_id={number.id}&status=promosso")
    assert r.status_code == 200, r.text
    ids = {c["id"] for c in r.json()["chat"]}
    assert ids == {gia_promosso.id}


@pytest.mark.asyncio
async def test_get_mostra_il_gruppo_ma_non_promuovibile(db_session, client):
    """Il gruppo resta VISIBILE nella lista (l'operatore deve vedere cosa lo
    scan ha trovato) ma con promuovibile=False, mai nascosto -- e mai
    promuovibile=True anche se un numero e' finito nella riga (bug del
    pannello, vincolo globale del piano)."""
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    gruppo_con_numero = await make_discovered_chat(
        db_session, tenant, number, tipo_chat="gruppo", chat_title="Famiglia Rossi")
    await db_session.commit()

    r = await client.get(f"/api/wa/discovered-chats?number_id={number.id}")
    assert r.status_code == 200, r.text
    righe = r.json()["chat"]
    assert len(righe) == 1
    assert righe[0]["id"] == gruppo_con_numero.id
    assert righe[0]["promuovibile"] is False


@pytest.mark.asyncio
async def test_get_numero_mascherato_mai_in_chiaro(db_session, client):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    riga = await make_discovered_chat(db_session, tenant, number, e164="+393421460077")
    await db_session.commit()

    r = await client.get(f"/api/wa/discovered-chats?number_id={number.id}")
    assert r.status_code == 200, r.text
    assert "3421460077" not in r.text
    assert "•" in r.text
    assert riga.id in {c["id"] for c in r.json()["chat"]}


@pytest.mark.asyncio
async def test_promote_con_id_valido_200_e_riga_passa_a_promosso(db_session, client):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    riga = await make_discovered_chat(db_session, tenant, number, chat_title="Mario Rossi",
                                      display_name="Mario Rossi")
    await db_session.commit()

    r = await client.post("/api/wa/discovered-chats/promote",
                          json={"number_id": number.id, "ids": [riga.id]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["promossi"] == 1
    assert body["contatti_creati"] == 1
    assert body["scarti"] == []
    assert len(body["contatti_promossi_ids"]) == 1

    riga_ricaricata = await db_session.get(WaDiscoveredChat, riga.id)
    assert riga_ricaricata.status == "promosso"
    contatto = await db_session.scalar(
        select(WaContact).where(WaContact.tenant_id == tenant.id,
                                WaContact.phone_hmac == riga.phone_hmac))
    assert contatto is not None


@pytest.mark.asyncio
async def test_promote_con_number_id_inesistente_404(db_session, client):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    riga = await make_discovered_chat(db_session, tenant, number)
    await db_session.commit()

    r = await client.post(
        "/api/wa/discovered-chats/promote",
        json={"number_id": "00000000-0000-0000-0000-000000000000", "ids": [riga.id]})
    assert r.status_code == 404, r.text

    riga_ricaricata = await db_session.get(WaDiscoveredChat, riga.id)
    assert riga_ricaricata.status == "nuovo"   # niente scritto


@pytest.mark.asyncio
async def test_promote_con_number_id_di_un_altro_tenant_non_promuove_riga_altrui(db_session, client):
    """Barriera IDOR (corretta in review Task 2): il tenant_id passato a
    `promuovi()` viene SEMPRE da `numero.tenant_id`, mai da un campo del
    client. Un `number_id` che risolve a un tenant diverso da quello
    proprietario della riga scoperta non deve poterla promuovere."""
    tenant_proprietario = await make_tenant(db_session, name="Proprietario")
    number_proprietario = await make_number(db_session, tenant_proprietario)
    riga = await make_discovered_chat(db_session, tenant_proprietario, number_proprietario)

    tenant_altrui = await make_tenant(db_session, name="Altro")
    number_altrui = await make_number(db_session, tenant_altrui)
    await db_session.commit()

    r = await client.post("/api/wa/discovered-chats/promote",
                          json={"number_id": number_altrui.id, "ids": [riga.id]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["promossi"] == 0
    assert body["scarti"] == [{"id": riga.id, "motivo": "non_trovato"}]

    riga_ricaricata = await db_session.get(WaDiscoveredChat, riga.id)
    assert riga_ricaricata.status == "nuovo"   # mai toccata
