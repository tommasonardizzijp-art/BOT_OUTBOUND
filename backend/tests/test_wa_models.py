import asyncio
import re
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models.tenant import Tenant, TenantStatus
from app.models.wa import (
    WaCampaign,
    WaCampaignContact,
    WaCampaignStatus,
    WaCampaignType,
    WaContact,
    WaNumber,
    WaNumberStatus,
    WaSequenceStep,
)
from app.utils.crypto import encrypt
from app.utils.db_dialect import to_async_database_url
from app.utils.phone_pseudonym import hmac_phone


async def _tenant(session: AsyncSession) -> Tenant:
    t = Tenant(id=str(uuid.uuid4()), name="Primero")
    session.add(t)
    await session.flush()
    return t


async def _numero(session: AsyncSession, tenant_id: str, e164: str) -> WaNumber:
    n = WaNumber(id=str(uuid.uuid4()), tenant_id=tenant_id, label="ADVWA-numero",
                 phone_hmac=hmac_phone(e164), encrypted_phone="x")
    session.add(n)
    await session.flush()
    return n


async def _campagna(session: AsyncSession, tenant_id: str, wa_number_id: str) -> WaCampaign:
    c = WaCampaign(id=str(uuid.uuid4()), tenant_id=tenant_id, wa_number_id=wa_number_id,
                    name="ADVWA-campagna", campaign_type=WaCampaignType.followup)
    session.add(c)
    await session.flush()
    return c


def _fresh_engine():
    """Engine indipendente (connessione propria), per i test di concorrenza
    e per gli invarianti finali via SQL: db_session (fixture in conftest.py)
    fa rollback a fine test per isolamento, qui invece serve stato REALMENTE
    committato e query indipendenti dalla sessione che ha scritto."""
    return create_async_engine(to_async_database_url(settings.database_url),
                                connect_args={"timeout": 30})


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


# ---------------------------------------------------------------------------
# Funzionale 11 -- wa_numbers.phone_hmac e' UNIQUE GLOBALE, non per-tenant
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phone_hmac_wa_numbers_unico_globale(db_session: AsyncSession):
    """Deliberatamente diverso da wa_contacts (funzionale 10): lo stesso
    numero MITTENTE non puo' essere registrato su due tenant, perche' e' un
    numero WhatsApp fisico che il bot controlla, non una rubrica per-cliente."""
    t1, t2 = await _tenant(db_session), await _tenant(db_session)
    h = hmac_phone("393421460077")
    db_session.add(WaNumber(id=str(uuid.uuid4()), tenant_id=t1.id, label="n1",
                            phone_hmac=h, encrypted_phone="x"))
    await db_session.flush()
    db_session.add(WaNumber(id=str(uuid.uuid4()), tenant_id=t2.id, label="n2",
                            phone_hmac=h, encrypted_phone="x"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


# ---------------------------------------------------------------------------
# Funzionale 12 -- UNIQUE(campaign_id, step_index) e (campaign_id, contact_id)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unique_campaign_step_index(db_session: AsyncSession):
    t = await _tenant(db_session)
    n = await _numero(db_session, t.id, "393421460078")
    camp = await _campagna(db_session, t.id, n.id)
    db_session.add(WaSequenceStep(id=str(uuid.uuid4()), campaign_id=camp.id,
                                  step_index=0, template_a="ciao"))
    await db_session.flush()
    db_session.add(WaSequenceStep(id=str(uuid.uuid4()), campaign_id=camp.id,
                                  step_index=0, template_a="ciao di nuovo"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_unique_campaign_contact(db_session: AsyncSession):
    t = await _tenant(db_session)
    n = await _numero(db_session, t.id, "393421460079")
    camp = await _campagna(db_session, t.id, n.id)
    contact = WaContact(id=str(uuid.uuid4()), tenant_id=t.id,
                        phone_hmac=hmac_phone("393421460080"), encrypted_phone="x")
    db_session.add(contact)
    await db_session.flush()
    db_session.add(WaCampaignContact(id=str(uuid.uuid4()), campaign_id=camp.id,
                                     contact_id=contact.id))
    await db_session.flush()
    db_session.add(WaCampaignContact(id=str(uuid.uuid4()), campaign_id=camp.id,
                                     contact_id=contact.id))
    with pytest.raises(IntegrityError):
        await db_session.flush()


# ---------------------------------------------------------------------------
# Funzionale 13 -- default rimasti scoperti (WaNumber.status e WaContact.opted_out
# sono gia' coperti sopra da test_numero_wa_nasce_in_pending_qr / test_contatto_nasce_contattabile)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_defaults_tenant_campagna_e_campaign_contact(db_session: AsyncSession):
    t = await _tenant(db_session)
    assert t.status == TenantStatus.active

    n = await _numero(db_session, t.id, "393421460081")
    camp = await _campagna(db_session, t.id, n.id)
    assert camp.optout_enabled is True
    assert camp.status == WaCampaignStatus.draft

    contact = WaContact(id=str(uuid.uuid4()), tenant_id=t.id,
                        phone_hmac=hmac_phone("393421460082"), encrypted_phone="x")
    db_session.add(contact)
    await db_session.flush()
    cc = WaCampaignContact(id=str(uuid.uuid4()), campaign_id=camp.id, contact_id=contact.id)
    db_session.add(cc)
    await db_session.flush()
    assert cc.current_step == -1


# ---------------------------------------------------------------------------
# Adversarial 27 -- concorrenza vera: due ingest dello stesso contatto in
# asyncio.gather, due sessioni/connessioni DISTINTE (non sequenziale).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_adv27_ingest_concorrente_stesso_contatto_unique_regge():
    """L'UNIQUE deve reggere sotto concorrenza vera: un solo record a fine
    gara, e l'errore di chi perde e' un IntegrityError pulito, non un 500 o
    un errore DB grezzo (es. 'database is locked')."""
    tenant_id = str(uuid.uuid4())
    h = hmac_phone("399" + "0001111")  # numero di test, prefisso 3990000xxxx

    setup_engine = _fresh_engine()
    try:
        maker = async_sessionmaker(setup_engine, expire_on_commit=False)
        async with maker() as s:
            s.add(Tenant(id=tenant_id, name="ADVWA-concorrenza"))
            await s.commit()
    finally:
        await setup_engine.dispose()

    async def _ingest():
        eng = _fresh_engine()
        try:
            maker = async_sessionmaker(eng, expire_on_commit=False)
            async with maker() as s:
                s.add(WaContact(id=str(uuid.uuid4()), tenant_id=tenant_id,
                                phone_hmac=h, encrypted_phone="x"))
                await s.commit()
            return "ok"
        finally:
            await eng.dispose()

    results = await asyncio.gather(_ingest(), _ingest(), return_exceptions=True)

    oks = [r for r in results if r == "ok"]
    integrity_errs = [r for r in results if isinstance(r, IntegrityError)]
    others = [r for r in results if r != "ok" and not isinstance(r, IntegrityError)]

    verify_engine = _fresh_engine()
    try:
        async with verify_engine.connect() as conn:
            count = (await conn.execute(
                text("SELECT COUNT(*) FROM wa_contacts WHERE tenant_id=:t AND phone_hmac=:h"),
                {"t": tenant_id, "h": h},
            )).scalar_one()
    finally:
        async with verify_engine.begin() as conn:
            await conn.execute(text("DELETE FROM wa_contacts WHERE tenant_id=:t"), {"t": tenant_id})
            await conn.execute(text("DELETE FROM tenants WHERE id=:t"), {"t": tenant_id})
        await verify_engine.dispose()

    assert not others, f"eccezione non pulita (non IntegrityError): {others!r}"
    assert len(oks) == 1 and len(integrity_errs) == 1, (
        f"attesi esattamente 1 successo + 1 IntegrityError, ottenuto: {results!r}"
    )
    assert count == 1, f"l'UNIQUE non ha retto: {count} righe invece di 1"


# ---------------------------------------------------------------------------
# Adversarial 28-29 -- tampering: phone_hmac scritto a mano (non calcolato da
# hmac_phone, per simulare un bug/tampering a monte) che collide.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_adv28_phone_hmac_collide_a_mano_stesso_tenant_rifiutato(db_session: AsyncSession):
    t = await _tenant(db_session)
    forged = "ADVWA0" + "f" * 58  # 64 char, non e' un vero HMAC ma il vincolo non lo sa
    db_session.add(WaContact(id=str(uuid.uuid4()), tenant_id=t.id, phone_hmac=forged,
                             encrypted_phone="x"))
    await db_session.flush()
    db_session.add(WaContact(id=str(uuid.uuid4()), tenant_id=t.id, phone_hmac=forged,
                             encrypted_phone="x"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_adv29_phone_hmac_wa_numbers_due_tenant_forged_rifiutato(db_session: AsyncSession):
    t1, t2 = await _tenant(db_session), await _tenant(db_session)
    forged = "ADVWA1" + "e" * 58
    db_session.add(WaNumber(id=str(uuid.uuid4()), tenant_id=t1.id, label="n1",
                            phone_hmac=forged, encrypted_phone="x"))
    await db_session.flush()
    db_session.add(WaNumber(id=str(uuid.uuid4()), tenant_id=t2.id, label="n2",
                            phone_hmac=forged, encrypted_phone="x"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


# ---------------------------------------------------------------------------
# Adversarial 30-31 -- integrita' referenziale: FK inesistente rifiutata.
# Cieco senza il PRAGMA foreign_keys=ON attivato in app/database.py (SS K).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_adv30_tenant_id_inesistente_rifiutato_da_fk(db_session: AsyncSession):
    ghost_tenant = "ADVWA-tenant-fantasma-" + str(uuid.uuid4())
    db_session.add(WaContact(id=str(uuid.uuid4()), tenant_id=ghost_tenant,
                             phone_hmac=hmac_phone("393421460083"), encrypted_phone="x"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_adv31_campaign_id_inesistente_rifiutato_da_fk(db_session: AsyncSession):
    t = await _tenant(db_session)
    contact = WaContact(id=str(uuid.uuid4()), tenant_id=t.id,
                        phone_hmac=hmac_phone("393421460084"), encrypted_phone="x")
    db_session.add(contact)
    await db_session.flush()
    ghost_campaign = "ADVWA-campaign-fantasma-" + str(uuid.uuid4())
    db_session.add(WaCampaignContact(id=str(uuid.uuid4()), campaign_id=ghost_campaign,
                                     contact_id=contact.id))
    with pytest.raises(IntegrityError):
        await db_session.flush()


# ---------------------------------------------------------------------------
# Adversarial 32 + invarianti finali via SQL (J: 44-46).
# Dati REALMENTE committati (non db_session, che fa rollback per isolamento):
# qui serve stato a disco interrogabile con query indipendenti dalla sessione
# che ha scritto, come le eseguirebbe un audit a fine run.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invarianti_finali_via_sql():
    """44: nessun duplicato (tenant_id, phone_hmac) in wa_contacts.
    45: nessun encrypted_phone che assomigli a un numero in chiaro.
    46: ogni phone_hmac (contatti + numeri) e' lungo 64 ed e' esadecimale.
    32: nessun chat_title che assomigli a un numero di telefono.

    Scritti con lo stesso percorso reale (normalize_e164 -> hmac_phone ->
    encrypt) usato dal resto del canale, non con valori finti: l'invariante
    deve reggere sull'uso vero, non solo su un caso costruito ad hoc."""
    tenant_id = str(uuid.uuid4())
    numero_e164 = "393990001122"
    h_contatto = hmac_phone(numero_e164)
    h_numero = hmac_phone("393990009999")
    enc = encrypt(numero_e164)

    plain_number_re = re.compile(r"^\+?[0-9]{8,15}$")               # J45
    hex64_re = re.compile(r"^[0-9a-f]{64}$")                        # J46
    chat_title_number_re = re.compile(r"^\+?[\d\s\-./()]{8,}$")     # adv32

    eng = _fresh_engine()
    try:
        maker = async_sessionmaker(eng, expire_on_commit=False)
        async with maker() as s:
            s.add(Tenant(id=tenant_id, name="ADVWA-invarianti"))
            s.add(WaContact(id=str(uuid.uuid4()), tenant_id=tenant_id, phone_hmac=h_contatto,
                            encrypted_phone=enc, chat_title=None))
            s.add(WaNumber(id=str(uuid.uuid4()), tenant_id=tenant_id, label="ADVWA-n",
                           phone_hmac=h_numero, encrypted_phone=enc))
            await s.commit()

        async with eng.connect() as conn:
            dup = (await conn.execute(text(
                "SELECT tenant_id, phone_hmac, COUNT(*) c FROM wa_contacts "
                "WHERE tenant_id=:t GROUP BY tenant_id, phone_hmac HAVING c > 1"
            ), {"t": tenant_id})).fetchall()
            assert dup == [], f"J44: duplicati (tenant_id, phone_hmac): {dup}"

            contatti = (await conn.execute(text(
                "SELECT encrypted_phone, phone_hmac, chat_title FROM wa_contacts WHERE tenant_id=:t"
            ), {"t": tenant_id})).fetchall()
            for enc_phone, phone_hmac, chat_title in contatti:
                assert not plain_number_re.match(enc_phone), (
                    f"J45: encrypted_phone assomiglia a un numero in chiaro: {enc_phone!r}"
                )
                assert hex64_re.match(phone_hmac), f"J46: phone_hmac non e' 64-hex: {phone_hmac!r}"
                if chat_title is not None:
                    assert not chat_title_number_re.match(chat_title), (
                        f"adv32: chat_title assomiglia a un numero: {chat_title!r}"
                    )

            numeri = (await conn.execute(text(
                "SELECT phone_hmac FROM wa_numbers WHERE tenant_id=:t"
            ), {"t": tenant_id})).fetchall()
            for (phone_hmac,) in numeri:
                assert hex64_re.match(phone_hmac), f"J46: phone_hmac non e' 64-hex: {phone_hmac!r}"
    finally:
        async with eng.begin() as conn:
            await conn.execute(text("DELETE FROM wa_contacts WHERE tenant_id=:t"), {"t": tenant_id})
            await conn.execute(text("DELETE FROM wa_numbers WHERE tenant_id=:t"), {"t": tenant_id})
            await conn.execute(text("DELETE FROM tenants WHERE id=:t"), {"t": tenant_id})
        await eng.dispose()
