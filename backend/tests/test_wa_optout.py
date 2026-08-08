import uuid

import pytest

from app.services import wa_optout


@pytest.mark.parametrize("testo", [
    "STOP",
    "stop",
    "Stop.",
    "  basta  ",
    "non scrivermi piu'",
    "CANCELLAMI da questa lista",
    "unsubscribe",
    "Va bene ma poi STOP grazie",
    "  STOP  ",   # adversarial #10: spazi iniziali/finali non impediscono il match
])
def test_looks_like_stop_riconosce_i_casi_plausibili(testo):
    assert wa_optout.looks_like_stop(testo) is True


@pytest.mark.parametrize("testo", [
    "",
    "ok grazie",
    "stopper",              # parola piu' lunga che CONTIENE stop
    "bastano due pezzi",    # 'bastano' non e' 'basta'
    "non scrivermi" ,       # NB: questo E' nella lista -> vedi test dedicato
])
def test_looks_like_stop_non_scatta_su_sottostringhe(testo):
    if testo == "non scrivermi":
        pytest.skip("frase presente nella lista: coperta dal test positivo")
    assert wa_optout.looks_like_stop(testo) is False


def test_looks_like_stop_su_none_non_solleva():
    """Finisce dentro una guardia: un'eccezione qui trasformerebbe un
    controllo di sicurezza in un crash che salta l'invio in modo casuale."""
    assert wa_optout.looks_like_stop(None) is False


@pytest.mark.parametrize("testo", [
    "mi basta sapere se siete aperti",
    "basta poco per convincermi",
    "basta che mi confermiate l'orario",
])
def test_looks_like_stop_basta_ambigua_in_frase_lunga_non_e_optout(testo):
    """Review G6 (07/08): 'basta' e' comunissima in frasi che non sono un
    opt-out. Un falso opt-out e' permanente e irreversibile -- il costo di
    sbagliare qui e' molto peggio del costo di chiedere una revisione umana."""
    assert wa_optout.looks_like_stop(testo) is False


@pytest.mark.parametrize("testo", [
    "mi basta sapere se siete aperti",
    "basta poco per convincermi",
])
def test_looks_like_ambiguous_stop_needs_review_su_frase_lunga(testo):
    assert wa_optout.looks_like_ambiguous_stop_needs_review(testo) is True


@pytest.mark.parametrize("testo", [
    "basta",
    "  basta  ",
    "ok grazie",
    "STOP",
    "",
])
def test_looks_like_ambiguous_stop_needs_review_non_scatta(testo):
    """Ne' su un 'basta' gia' gestito da looks_like_stop (corto, opt-out
    diretto), ne' su testi senza la parola ambigua."""
    assert wa_optout.looks_like_ambiguous_stop_needs_review(testo) is False


def test_looks_like_ambiguous_stop_needs_review_su_none_non_solleva():
    assert wa_optout.looks_like_ambiguous_stop_needs_review(None) is False


@pytest.mark.asyncio
async def test_persist_wa_optout_ferma_tutte_le_campagne_del_tenant(db_session):
    from app.models.tenant import Tenant
    from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                               WaCampaignType, WaContact, WaContactStatus,
                               WaDncReason, WaNumber, WaSendCondition, WaSequenceStep)

    tenant = Tenant(id=str(uuid.uuid4()), name="T", status="active")
    db_session.add(tenant)
    await db_session.flush()
    number = WaNumber(id=str(uuid.uuid4()), tenant_id=tenant.id, label="n",
                      phone_hmac=f"h-{uuid.uuid4()}", encrypted_phone="e")
    contact = WaContact(id=str(uuid.uuid4()), tenant_id=tenant.id,
                        phone_hmac=f"c-{uuid.uuid4()}", encrypted_phone="e")
    db_session.add_all([number, contact])
    await db_session.flush()
    # DUE campagne diverse dello stesso tenant: l'opt-out le ferma entrambe.
    righe = []
    for nome in ("camp-A", "camp-B"):
        camp = WaCampaign(id=str(uuid.uuid4()), tenant_id=tenant.id,
                          wa_number_id=number.id, name=nome,
                          campaign_type=WaCampaignType.marketing,
                          status=WaCampaignStatus.running)
        db_session.add(camp)
        await db_session.flush()
        cc = WaCampaignContact(id=str(uuid.uuid4()), campaign_id=camp.id,
                               contact_id=contact.id, status=WaContactStatus.queued)
        db_session.add(cc)
        righe.append(cc)
    await db_session.commit()

    fermate = await wa_optout.persist_wa_optout(
        db_session, contact.id, prova="STOP")
    assert fermate == 2

    await db_session.refresh(contact)
    assert contact.opted_out is True
    assert contact.do_not_contact is True
    assert contact.dnc_reason == WaDncReason.optout
    assert contact.opted_out_at is not None
    for cc in righe:
        await db_session.refresh(cc)
        assert cc.status == WaContactStatus.opted_out


async def _campagna_con_conteggi(db_session, *, sent: int, opted_out: int):
    from app.models.tenant import Tenant
    from app.models.wa import (WaCampaign, WaCampaignType, WaCampaignStatus,
                               WaNumber)

    tenant = Tenant(id=str(uuid.uuid4()), name="T-breaker", status="active")
    db_session.add(tenant)
    await db_session.flush()
    number = WaNumber(id=str(uuid.uuid4()), tenant_id=tenant.id, label="n",
                      phone_hmac=f"h-{uuid.uuid4()}", encrypted_phone="e")
    db_session.add(number)
    await db_session.flush()
    campagna = WaCampaign(id=str(uuid.uuid4()), tenant_id=tenant.id,
                          wa_number_id=number.id, name="camp-breaker",
                          campaign_type=WaCampaignType.marketing,
                          status=WaCampaignStatus.running,
                          sent=sent, opted_out=opted_out)
    db_session.add(campagna)
    await db_session.commit()
    return campagna


@pytest.mark.asyncio
async def test_circuit_breaker_muto_sotto_il_minimo_di_invii(db_session):
    """1 opt-out su 2 invii e' il 50%, ma il campione e' troppo piccolo per
    significare qualcosa: il breaker non deve scattare su rumore statistico."""
    from app.services import bot_state_service

    campagna = await _campagna_con_conteggi(db_session, sent=2, opted_out=1)
    scattato = await wa_optout.check_optout_circuit_breaker(db_session, campagna.id)
    assert scattato is False
    assert await bot_state_service.is_wa_halted(db_session) is False


@pytest.mark.asyncio
async def test_circuit_breaker_muto_sotto_la_soglia_percentuale(db_session):
    from app.services import bot_state_service

    campagna = await _campagna_con_conteggi(db_session, sent=20, opted_out=2)  # 10%
    scattato = await wa_optout.check_optout_circuit_breaker(db_session, campagna.id)
    assert scattato is False
    assert await bot_state_service.is_wa_halted(db_session) is False


@pytest.mark.asyncio
async def test_circuit_breaker_ferma_il_canale_sopra_soglia(db_session):
    """Il numero che rischia il ban e' del cliente: sopra soglia il breaker
    ferma l'INTERO canale (stesso kill-switch di POST /wa/ops/halt), non
    solo la campagna."""
    from app.services import bot_state_service

    campagna = await _campagna_con_conteggi(db_session, sent=20, opted_out=6)  # 30%
    scattato = await wa_optout.check_optout_circuit_breaker(db_session, campagna.id)
    assert scattato is True
    assert await bot_state_service.is_wa_halted(db_session) is True


@pytest.mark.asyncio
async def test_circuit_breaker_non_riallerta_se_gia_fermo(db_session):
    """Idempotente: una volta fermo il canale, opt-out successivi non
    devono rifermare/riallertare da capo (spam Telegram a ogni opt-out)."""
    from app.services import bot_state_service

    campagna = await _campagna_con_conteggi(db_session, sent=20, opted_out=6)
    primo = await wa_optout.check_optout_circuit_breaker(db_session, campagna.id)
    secondo = await wa_optout.check_optout_circuit_breaker(db_session, campagna.id)
    assert primo is True
    assert secondo is False


@pytest.mark.asyncio
async def test_circuit_breaker_scrive_lo_stop_a_DB_non_solo_in_sessione(db_session):
    """Il breaker deve PERSISTERE lo stop, non lasciarlo pendente.

    Gli altri test del breaker interrogano `is_wa_halted` sulla STESSA
    sessione con cui il breaker ha scritto: l'autoflush di SQLAlchemy mostra
    la modifica pendente come se fosse gia' a DB, quindi restano verdi anche
    se il commit non avviene mai. In produzione a leggere e' un'altra
    sessione -- il worker ne apre una nuova a ogni messaggio
    (`wa_worker.py`, `async with AsyncSessionLocal()`) e chiama
    `is_wa_halted()` senza argomenti. Se lo stop non e' committato, quella
    lettura non lo vede e il canale continua a inviare mentre il Telegram
    dice "FERMATO in automatico".

    Qui si legge da una connessione DIVERSA: e' l'unico modo di distinguere
    "scritto" da "scritto e salvato".
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import settings
    from app.services import bot_state_service
    from app.utils.db_dialect import to_async_database_url

    campagna = await _campagna_con_conteggi(db_session, sent=20, opted_out=6)  # 30%
    try:
        assert await wa_optout.check_optout_circuit_breaker(db_session, campagna.id) is True

        # Il worker non committa dopo il breaker: esce dal `async with` e la
        # sessione si chiude, buttando via il pendente. Qui lo si riproduce
        # con un rollback esplicito -- che e' anche cio' che fa il suo
        # except quando l'invio solleva. Se il breaker ha committato, lo stop
        # sopravvive; se ha solo sporcato la sessione, sparisce qui.
        await db_session.rollback()

        altro_engine = create_async_engine(to_async_database_url(settings.database_url))
        try:
            async with async_sessionmaker(altro_engine, expire_on_commit=False)() as altra_sessione:
                assert await bot_state_service.is_wa_halted(altra_sessione) is True
        finally:
            await altro_engine.dispose()
    finally:
        # Il canale fermo e' stato COMMITTATO: senza questo ripristino resta
        # fermo per tutti i test che girano dopo, in qualunque file.
        await bot_state_service.resume_wa(by="test-cleanup")


@pytest.mark.asyncio
async def test_circuit_breaker_su_campaign_id_none_non_solleva(db_session):
    assert await wa_optout.check_optout_circuit_breaker(db_session, None) is False


@pytest.mark.asyncio
async def test_persist_wa_optout_e_idempotente(db_session):
    """Un secondo STAOP sullo stesso contatto non deve rimettere in
    opted_out righe gia' terminali ne' contarle di nuovo."""
    from app.models.tenant import Tenant
    from app.models.wa import WaContact

    tenant = Tenant(id=str(uuid.uuid4()), name="T2", status="active")
    db_session.add(tenant)
    await db_session.flush()
    contact = WaContact(id=str(uuid.uuid4()), tenant_id=tenant.id,
                        phone_hmac=f"c-{uuid.uuid4()}", encrypted_phone="e")
    db_session.add(contact)
    await db_session.commit()

    primo = await wa_optout.persist_wa_optout(db_session, contact.id, prova="stop")
    secondo = await wa_optout.persist_wa_optout(db_session, contact.id, prova="stop")
    assert primo == 0 and secondo == 0
    await db_session.refresh(contact)
    assert contact.opted_out is True
