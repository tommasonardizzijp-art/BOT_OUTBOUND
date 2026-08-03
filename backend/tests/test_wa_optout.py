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
