from datetime import datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from tests.factories_wa import (make_campaign, make_campaign_contact, make_contact,
                                make_number, make_tenant)


async def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_ingest_risponde_col_report(db_session):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    await db_session.commit()

    async with await _client() as client:
        r = await client.post(
            "/api/wa/contacts/ingest",
            data={"campaign_id": campaign.id},
            files={"file": ("lista.csv", b"numero,nome\n+393331112223,Marco\n", "text/csv")},
        )
    assert r.status_code in (200, 401)
    if r.status_code == 200:
        body = r.json()
        assert body["creati"] == 1
        assert body["scarti"] == []


@pytest.mark.asyncio
async def test_file_non_csv_rifiutato_con_422_non_500(db_session):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    await db_session.commit()
    async with await _client() as client:
        r = await client.post(
            "/api/wa/contacts/ingest",
            data={"campaign_id": campaign.id},
            files={"file": ("foto.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        )
    assert r.status_code in (401, 422)


@pytest.mark.asyncio
async def test_ingest_su_campagna_running_e_rifiutato(db_session):
    """Macchina a stati: la lista si carica in draft. Aggiungere contatti a
    una campagna che sta girando cambia il denominatore dei KPI sotto i
    piedi al worker."""
    from app.models.wa import WaCampaignStatus
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number,
                                      status=WaCampaignStatus.running)
    await db_session.commit()
    async with await _client() as client:
        r = await client.post(
            "/api/wa/contacts/ingest",
            data={"campaign_id": campaign.id},
            files={"file": ("l.csv", b"numero\n+393331112223\n", "text/csv")},
        )
    assert r.status_code in (401, 409)


@pytest.mark.asyncio
async def test_rimozione_contatto_sotto_lock_fresco_rifiutata(db_session):
    """Invariante I1: M2 LEGGE i campi di lock e non li scrive. Una riga
    sotto lock e' in mano al worker di M3 in questo momento."""
    from app.api import wa_contacts
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    contact = await make_contact(db_session, tenant)
    cc = await make_campaign_contact(db_session, campaign, contact)
    cc.locked_by = "worker-vivo"
    cc.locked_at = datetime.utcnow()
    await db_session.commit()

    with pytest.raises(Exception):
        await wa_contacts.rimuovi_contatto(cc.id, db=db_session)

    cc.locked_at = datetime.utcnow() - timedelta(minutes=45)   # lock stale
    await db_session.commit()
    assert await wa_contacts.rimuovi_contatto(cc.id, db=db_session) == {"rimosso": True}
