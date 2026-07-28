import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import Tenant
from app.models.wa import WaContact, WaNumber, WaNumberStatus
from app.utils.phone_pseudonym import hmac_phone


async def _tenant(session: AsyncSession) -> Tenant:
    t = Tenant(id=str(uuid.uuid4()), name="Primero")
    session.add(t)
    await session.flush()
    return t


@pytest.mark.asyncio
async def test_contatto_unico_per_tenant_e_numero(db_session: AsyncSession):
    """UNIQUE(tenant_id, phone_hmac): due tenant possono avere lo stesso
    contatto, lo stesso tenant no. Senza questo, un doppio upload del CSV
    crea due contatti e la persona riceve il messaggio due volte."""
    t = await _tenant(db_session)
    h = hmac_phone("393421460077")
    db_session.add(WaContact(id=str(uuid.uuid4()), tenant_id=t.id, phone_hmac=h,
                             encrypted_phone="x"))
    await db_session.flush()
    db_session.add(WaContact(id=str(uuid.uuid4()), tenant_id=t.id, phone_hmac=h,
                             encrypted_phone="x"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_stesso_numero_su_due_tenant_e_permesso(db_session: AsyncSession):
    t1, t2 = await _tenant(db_session), await _tenant(db_session)
    h = hmac_phone("393421460077")
    db_session.add_all([
        WaContact(id=str(uuid.uuid4()), tenant_id=t1.id, phone_hmac=h, encrypted_phone="x"),
        WaContact(id=str(uuid.uuid4()), tenant_id=t2.id, phone_hmac=h, encrypted_phone="x"),
    ])
    await db_session.flush()   # nessuna eccezione: sono clienti diversi


@pytest.mark.asyncio
async def test_numero_wa_nasce_in_pending_qr(db_session: AsyncSession):
    t = await _tenant(db_session)
    n = WaNumber(id=str(uuid.uuid4()), tenant_id=t.id, label="Primero sede",
                 phone_hmac=hmac_phone("393421460077"), encrypted_phone="x")
    db_session.add(n)
    await db_session.flush()
    assert n.status == WaNumberStatus.pending_qr
    assert n.sent_today == 0


@pytest.mark.asyncio
async def test_contatto_nasce_contattabile(db_session: AsyncSession):
    t = await _tenant(db_session)
    c = WaContact(id=str(uuid.uuid4()), tenant_id=t.id,
                  phone_hmac=hmac_phone("393421460077"), encrypted_phone="x")
    db_session.add(c)
    await db_session.flush()
    assert c.opted_out is False and c.do_not_contact is False


@pytest.mark.asyncio
async def test_chat_title_e_nullable(db_session: AsyncSession):
    """chat_title resta NULL quando il titolo della chat e' un NUMERO (contatto
    non in rubrica del cliente): salvarlo metterebbe il numero in chiaro a DB,
    violando P12. In quel caso il matching usa gia' phone_hmac."""
    t = await _tenant(db_session)
    c = WaContact(id=str(uuid.uuid4()), tenant_id=t.id,
                  phone_hmac=hmac_phone("393421460077"), encrypted_phone="x")
    db_session.add(c)
    await db_session.flush()
    assert c.chat_title is None
