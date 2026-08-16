"""GET /wa/contacts/disponibili -- i contatti gia' in rubbrica arruolabili.

Perche' esiste questa rotta: `GET /wa/contacts` elenca i contatti DI UNA
CAMPAGNA, non quelli che si potrebbero ancora aggiungere. Senza questa, il
passo 2 del wizard poteva solo caricare un CSV -- anche quando i contatti
erano gia' a DB, scoperti dall'auto-discover.

Il vincolo che modella l'ambito: `WaContact` NON ha un `wa_number_id`. I
contatti stanno sul tenant (UNIQUE su tenant_id+phone_hmac), non sul numero.
"I contatti di questo numero" esiste solo indirettamente, via
`wa_discovered_chats.number_id` con lo stesso `phone_hmac` -- ed e' per
questo che l'ambito e' un parametro esplicito e non un'assunzione.
"""
from datetime import datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.database import get_db
from app.main import app
from app.models.user import User
from app.models.wa import WaDncReason
from app.utils.auth_deps import get_current_user
from tests.factories_wa import (make_campaign, make_campaign_contact,
                                make_contact, make_discovered_chat,
                                make_number, make_tenant)


def _admin_utente() -> User:
    return User(id="00000000-0000-0000-0000-00000000000d",
                email="admin-wa-disp@test.local", password_hash="x",
                role="admin", is_active=True, created_at=datetime.utcnow())


@pytest_asyncio.fixture
async def client(db_session):
    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _admin_utente
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _scenario(db):
    """Tenant con due numeri, una campagna sul primo, e contatti misti."""
    tenant = await make_tenant(db)
    numero_a = await make_number(db, tenant, label="Numero A")
    numero_b = await make_number(db, tenant, label="Numero B")
    campagna, _step = await make_campaign(db, tenant, numero_a)

    # scoperto su A -> deve comparire con ambito=numero
    su_a = await make_contact(db, tenant, e164="+393330000001", display_name="Da A")
    await make_discovered_chat(db, tenant, numero_a, e164="+393330000001",
                               chat_title="Chat A", status="promosso")

    # scoperto su B -> NON deve comparire con ambito=numero
    su_b = await make_contact(db, tenant, e164="+393330000002", display_name="Da B")
    await make_discovered_chat(db, tenant, numero_b, e164="+393330000002",
                               chat_title="Chat B", status="promosso")

    # nessuna chat scoperta (arrivato da CSV) -> solo con ambito=tutti
    da_csv = await make_contact(db, tenant, e164="+393330000003", display_name="Da CSV")

    await db.commit()
    return tenant, numero_a, campagna, su_a, su_b, da_csv


@pytest.mark.asyncio
async def test_ambito_numero_torna_solo_i_contatti_scoperti_su_quel_numero(
        client, db_session):
    _, _, campagna, su_a, su_b, da_csv = await _scenario(db_session)

    r = await client.get("/api/wa/contacts/disponibili",
                         params={"campaign_id": campagna.id, "ambito": "numero"})
    assert r.status_code == 200
    ids = {c["id"] for c in r.json()["contatti"]}
    assert ids == {su_a.id}


@pytest.mark.asyncio
async def test_ambito_tutti_torna_tutta_la_rubrica_del_tenant(client, db_session):
    _, _, campagna, su_a, su_b, da_csv = await _scenario(db_session)

    r = await client.get("/api/wa/contacts/disponibili",
                         params={"campaign_id": campagna.id, "ambito": "tutti"})
    assert r.status_code == 200
    ids = {c["id"] for c in r.json()["contatti"]}
    assert ids == {su_a.id, su_b.id, da_csv.id}


@pytest.mark.asyncio
async def test_i_contatti_di_un_altro_tenant_non_compaiono(client, db_session):
    _, _, campagna, su_a, _, _ = await _scenario(db_session)
    estraneo = await make_tenant(db_session, name="Tenant Estraneo")
    intruso = await make_contact(db_session, estraneo, e164="+393339999999",
                                 display_name="Intruso")
    await db_session.commit()

    r = await client.get("/api/wa/contacts/disponibili",
                         params={"campaign_id": campagna.id, "ambito": "tutti"})
    ids = {c["id"] for c in r.json()["contatti"]}
    assert intruso.id not in ids


@pytest.mark.asyncio
async def test_chi_e_gia_nella_campagna_e_escluso_e_contato(client, db_session):
    _, _, campagna, su_a, _, _ = await _scenario(db_session)
    await make_campaign_contact(db_session, campagna, su_a)
    await db_session.commit()

    r = await client.get("/api/wa/contacts/disponibili",
                         params={"campaign_id": campagna.id, "ambito": "numero"})
    corpo = r.json()
    assert [c["id"] for c in corpo["contatti"]] == []
    assert corpo["esclusi"]["gia_in_campagna"] == 1


@pytest.mark.asyncio
async def test_opt_out_e_dnc_sono_esclusi_e_contati(client, db_session):
    _, _, campagna, su_a, su_b, da_csv = await _scenario(db_session)
    su_b.opted_out = True
    da_csv.do_not_contact = True
    da_csv.dnc_reason = WaDncReason.manual
    await db_session.commit()

    r = await client.get("/api/wa/contacts/disponibili",
                         params={"campaign_id": campagna.id, "ambito": "tutti"})
    corpo = r.json()
    assert [c["id"] for c in corpo["contatti"]] == [su_a.id]
    assert corpo["esclusi"]["opt_out_o_dnc"] == 2


@pytest.mark.asyncio
async def test_il_totale_non_e_limitato_dalla_pagina(client, db_session):
    """`totale_disponibili` deve contare TUTTI gli arruolabili, non la pagina.

    Serve alla UI per dire "arruola tutti i N" senza scorrere le pagine: se
    tornasse la lunghezza della pagina, il bottone mentirebbe sul numero.
    """
    _, _, campagna, su_a, su_b, da_csv = await _scenario(db_session)

    r = await client.get("/api/wa/contacts/disponibili",
                         params={"campaign_id": campagna.id, "ambito": "tutti",
                                 "limit": 1})
    corpo = r.json()
    assert len(corpo["contatti"]) == 1
    assert corpo["totale_disponibili"] == 3


@pytest.mark.asyncio
async def test_il_numero_torna_mascherato(client, db_session):
    """P12, stesso vincolo di lista_contatti: mai il numero intero."""
    _, _, campagna, _, _, _ = await _scenario(db_session)

    r = await client.get("/api/wa/contacts/disponibili",
                         params={"campaign_id": campagna.id, "ambito": "tutti"})
    for c in r.json()["contatti"]:
        assert "0000000" not in c["numero"]
        assert "•" in c["numero"] or "*" in c["numero"]


@pytest.mark.asyncio
async def test_campagna_inesistente_404(client):
    r = await client.get("/api/wa/contacts/disponibili",
                         params={"campaign_id": "non-esiste", "ambito": "tutti"})
    assert r.status_code == 404
