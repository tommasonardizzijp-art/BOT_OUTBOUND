"""M5: revoca opt-out (POST /wa/ops/contacts/{contact_id}/revoke-optout).

Un contatto che ha scritto STOP e poi cambia idea (es. chiama e chiede di
essere ricontattato) oggi non ha modo di essere riabilitato se non a mano
sul DB. Questo endpoint lo fa in modo tracciato: nota obbligatoria (GDPR/
ePrivacy: revocare una preferenza di opt-out espressa va sempre
giustificato e loggato), e MAI resuscita righe wa_campaign_contacts gia'
terminali di campagne passate -- la revoca vale solo in avanti.

Pattern di fixture: stesso di test_wa_ops_api.py (chiamata diretta alle
funzioni async con db_session per la logica) + client HTTP con get_db
overridden sulla stessa sessione (pattern di test_wa_api_contacts.py) per
i casi che devono passare per la validazione Pydantic del body.
"""
from datetime import datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.database import get_db
from app.main import app
from app.models.user import User
from app.utils.auth_deps import get_current_user
from tests.factories_wa import (make_campaign, make_campaign_contact,
                                make_contact, make_number, make_tenant)


def _admin_utente() -> User:
    return User(id="00000000-0000-0000-0000-00000000000c", email="admin-wa-revoke@test.local",
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


async def _contatto_in_optout(db_session, *, dnc_reason):
    """Crea un tenant+contatto gia' marcato opted_out, con un dnc_reason
    a scelta (per coprire sia il caso optout 'puro' che quello sovrascritto
    da un altro processo, es. manual)."""
    from app.models.wa import WaDncReason

    tenant = await make_tenant(db_session)
    contact = await make_contact(db_session, tenant)
    contact.opted_out = True
    contact.opted_out_at = datetime.utcnow()
    contact.do_not_contact = True
    contact.dnc_reason = dnc_reason
    await db_session.commit()
    return tenant, contact


# ---------------------------------------------------------------------------
# Logica core (chiamata diretta, stesso stile di test_wa_ops_api.py)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_revoca_su_optout_da_stop_pulisce_tutto(db_session):
    from app.api import wa_ops
    from app.models.wa import WaDncReason

    _, contact = await _contatto_in_optout(db_session, dnc_reason=WaDncReason.optout)

    esito = await wa_ops.revoke_optout(contact.id, body=wa_ops.RevokeOptoutRequest(
        note="Ha richiamato, chiede di essere ricontattato"), db=db_session)
    assert esito == {"revoked": True}

    await db_session.refresh(contact)
    assert contact.opted_out is False
    assert contact.opted_out_at is None
    assert contact.do_not_contact is False
    assert contact.dnc_reason is None


@pytest.mark.asyncio
async def test_revoca_con_dnc_reason_diverso_non_tocca_do_not_contact(db_session):
    """Il punto piu' delicato: se il dnc_reason e' stato sovrascritto a una
    causa DNC indipendente dallo STOP (es. manual), la revoca dell'opt-out
    NON deve riabilitare il contatto -- quella causa resta valida a
    prescindere."""
    from app.api import wa_ops
    from app.models.wa import WaDncReason

    _, contact = await _contatto_in_optout(db_session, dnc_reason=WaDncReason.manual)

    esito = await wa_ops.revoke_optout(contact.id, body=wa_ops.RevokeOptoutRequest(
        note="Revoco solo lo STOP, ma il DNC manuale resta"), db=db_session)
    assert esito == {"revoked": True}

    await db_session.refresh(contact)
    assert contact.opted_out is False
    assert contact.opted_out_at is None
    # dnc_reason 'manual' e do_not_contact restano intatti: causa indipendente.
    assert contact.do_not_contact is True
    assert contact.dnc_reason == WaDncReason.manual


@pytest.mark.asyncio
@pytest.mark.parametrize("dnc_reason", ["unreachable", "invalid_number"])
async def test_revoca_con_altri_dnc_reason_indipendenti_non_tocca_do_not_contact(db_session, dnc_reason):
    from app.api import wa_ops
    from app.models.wa import WaDncReason

    _, contact = await _contatto_in_optout(db_session, dnc_reason=WaDncReason(dnc_reason))

    await wa_ops.revoke_optout(contact.id, body=wa_ops.RevokeOptoutRequest(
        note="test causa indipendente"), db=db_session)

    await db_session.refresh(contact)
    assert contact.opted_out is False
    assert contact.do_not_contact is True
    assert contact.dnc_reason == WaDncReason(dnc_reason)


@pytest.mark.asyncio
async def test_revoca_su_contatto_non_in_optout_e_no_op(db_session):
    tenant = await make_tenant(db_session)
    contact = await make_contact(db_session, tenant)
    await db_session.commit()
    assert contact.opted_out is False

    from app.api import wa_ops

    esito = await wa_ops.revoke_optout(contact.id, body=wa_ops.RevokeOptoutRequest(
        note="tentativo su contatto pulito"), db=db_session)
    assert esito == {"revoked": False, "motivo": "contatto non era in opt-out"}

    await db_session.refresh(contact)
    assert contact.opted_out is False
    assert contact.do_not_contact is False
    assert contact.dnc_reason is None


@pytest.mark.asyncio
async def test_revoca_su_contatto_inesistente_torna_404(db_session):
    import uuid
    from fastapi import HTTPException
    from app.api import wa_ops

    with pytest.raises(HTTPException) as exc:
        await wa_ops.revoke_optout(str(uuid.uuid4()), body=wa_ops.RevokeOptoutRequest(
            note="qualunque nota"), db=db_session)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_revoca_non_tocca_righe_campaign_contact_gia_terminali(db_session):
    """Storico corretto: righe wa_campaign_contacts gia' opted_out di una
    campagna passata restano opted_out. La revoca vale solo in avanti (nuovo
    ingest), non resuscita invii su campagne gia' fermate."""
    from app.api import wa_ops
    from app.models.wa import WaContactStatus, WaDncReason

    tenant, contact = await _contatto_in_optout(db_session, dnc_reason=WaDncReason.optout)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    cc = await make_campaign_contact(db_session, campaign, contact,
                                     status=WaContactStatus.opted_out)
    await db_session.commit()

    await wa_ops.revoke_optout(contact.id, body=wa_ops.RevokeOptoutRequest(
        note="revoca dopo richiamata"), db=db_session)

    await db_session.refresh(cc)
    assert cc.status == WaContactStatus.opted_out


# ---------------------------------------------------------------------------
# HTTP layer: validazione body, 404, roundtrip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_http_revoca_ok(db_session, client):
    from app.models.wa import WaDncReason

    _, contact = await _contatto_in_optout(db_session, dnc_reason=WaDncReason.optout)

    r = await client.post(f"/api/wa/ops/contacts/{contact.id}/revoke-optout",
                          json={"note": "richiesta telefonica del cliente"})
    assert r.status_code == 200, r.text
    assert r.json() == {"revoked": True}


@pytest.mark.asyncio
async def test_http_revoca_contatto_inesistente_e_404(client):
    import uuid
    r = await client.post(f"/api/wa/ops/contacts/{uuid.uuid4()}/revoke-optout",
                          json={"note": "nota qualsiasi"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_http_revoca_senza_note_e_422(db_session, client):
    from app.models.wa import WaDncReason

    _, contact = await _contatto_in_optout(db_session, dnc_reason=WaDncReason.optout)
    r = await client.post(f"/api/wa/ops/contacts/{contact.id}/revoke-optout", json={})
    assert r.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("nota", ["", "   ", "\t\n"])
async def test_http_revoca_con_note_vuota_o_whitespace_e_422(db_session, client, nota):
    from app.models.wa import WaDncReason

    _, contact = await _contatto_in_optout(db_session, dnc_reason=WaDncReason.optout)
    r = await client.post(f"/api/wa/ops/contacts/{contact.id}/revoke-optout",
                          json={"note": nota})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_revoca_due_volte_di_fila_seconda_e_no_op(db_session):
    """Idempotenza: la prima chiamata revoca davvero, la seconda trova il
    contatto gia' pulito e risponde no-op senza toccare nulla."""
    from app.api import wa_ops
    from app.models.wa import WaDncReason

    _, contact = await _contatto_in_optout(db_session, dnc_reason=WaDncReason.optout)

    primo = await wa_ops.revoke_optout(contact.id, body=wa_ops.RevokeOptoutRequest(
        note="prima revoca"), db=db_session)
    assert primo == {"revoked": True}

    secondo = await wa_ops.revoke_optout(contact.id, body=wa_ops.RevokeOptoutRequest(
        note="seconda revoca, non dovrebbe fare nulla"), db=db_session)
    assert secondo == {"revoked": False, "motivo": "contatto non era in opt-out"}
