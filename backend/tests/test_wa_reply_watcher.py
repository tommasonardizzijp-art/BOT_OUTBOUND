import pytest
from sqlalchemy import select

from app.browser.whatsapp_page import ChatRow
from app.models.wa import WaMatchedBy
from tests.factories_wa import make_contact, make_tenant


def _row(title, *, title_is_number=False, preview="ciao", unread=1):
    return ChatRow(position=0, title=title, title_is_number=title_is_number,
                   unread_count=unread, preview=preview, last_is_outbound=False,
                   outgoing_state=None, muted=False)


@pytest.mark.asyncio
async def test_match_per_chat_title(db_session):
    from app.services.wa_reply_watcher import match_contact

    tenant = await make_tenant(db_session)
    contatto = await make_contact(db_session, tenant, display_name="Marco")
    contatto.chat_title = "Marco Rossi"
    await db_session.commit()

    trovato, via = await match_contact(db_session, tenant.id, _row("Marco Rossi"))
    assert trovato.id == contatto.id
    assert via == WaMatchedBy.chat_title


@pytest.mark.asyncio
async def test_match_per_numero(db_session):
    from app.utils.phone_pseudonym import hmac_phone
    from app.services.wa_reply_watcher import match_contact

    tenant = await make_tenant(db_session)
    contatto = await make_contact(db_session, tenant, e164="+393331234567")
    await db_session.commit()

    row = _row("+39 333 1234567", title_is_number=True)
    trovato, via = await match_contact(db_session, tenant.id, row)
    assert trovato.id == contatto.id
    assert via == WaMatchedBy.phone


@pytest.mark.asyncio
async def test_nessun_match(db_session):
    from app.services.wa_reply_watcher import match_contact

    tenant = await make_tenant(db_session)
    trovato, via = await match_contact(db_session, tenant.id, _row("Sconosciuto"))
    assert trovato is None
    assert via == WaMatchedBy.none


@pytest.mark.asyncio
async def test_title_ambiguo_mai_indovinare(db_session):
    """Due contatti con lo stesso chat_title nel tenant: il matching per
    title si disabilita per quel title, mai un match a caso (SDD 7.3)."""
    from app.services.wa_reply_watcher import match_contact

    tenant = await make_tenant(db_session)
    c1 = await make_contact(db_session, tenant, e164="+393330000001")
    c1.chat_title = "Marco"
    c2 = await make_contact(db_session, tenant, e164="+393330000002")
    c2.chat_title = "Marco"
    await db_session.commit()

    trovato, via = await match_contact(db_session, tenant.id, _row("Marco"))
    assert trovato is None
    assert via == WaMatchedBy.none


@pytest.mark.asyncio
async def test_process_row_optout(db_session):
    from app.services.wa_reply_watcher import process_chat_row
    from app.models.wa import WaContact
    from tests.factories_wa import make_number

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    contatto.chat_title = "Marco"
    await db_session.commit()

    esito = await process_chat_row(
        db_session, tenant_id=tenant.id, wa_number_id=numero.id,
        row=_row("Marco", preview="basta scrivermi"))
    assert esito["esito"] == "optout"

    await db_session.refresh(contatto)
    assert contatto.opted_out is True
    assert contatto.do_not_contact is True


@pytest.mark.asyncio
async def test_process_row_replied(db_session):
    from app.services.wa_reply_watcher import process_chat_row
    from app.models.wa import WaCampaignContact, WaContactStatus
    from tests.factories_wa import make_campaign, make_campaign_contact, make_number

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    contatto.chat_title = "Marco"
    campagna, step = await make_campaign(db_session, tenant, numero)
    cc = await make_campaign_contact(db_session, campagna, contatto,
                                      status=WaContactStatus.in_sequence, current_step=0)
    await db_session.commit()

    esito = await process_chat_row(
        db_session, tenant_id=tenant.id, wa_number_id=numero.id,
        row=_row("Marco", preview="si mi interessa"))
    assert esito["esito"] == "replied"

    await db_session.refresh(cc)
    assert cc.status == WaContactStatus.replied
    assert cc.replied_at_step == 0


@pytest.mark.asyncio
async def test_process_row_dedup_su_ultimo_evento(db_session):
    """Stessa preview del contatto gia' vista -> nessun secondo evento,
    nessuna doppia scrittura (SDD 7.3, dedup)."""
    from app.services.wa_reply_watcher import process_chat_row
    from app.models.wa import WaInboundEvent
    from tests.factories_wa import make_number

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    contatto.chat_title = "Marco"
    await db_session.commit()

    riga = _row("Marco", preview="si mi interessa")
    primo = await process_chat_row(db_session, tenant_id=tenant.id,
                                   wa_number_id=numero.id, row=riga)
    secondo = await process_chat_row(db_session, tenant_id=tenant.id,
                                     wa_number_id=numero.id, row=riga)
    assert primo["esito"] in ("replied", "non_associato", "ignorato")
    assert secondo["esito"] == "duplicato"

    eventi = (await db_session.execute(
        select(WaInboundEvent).where(WaInboundEvent.contact_id == contatto.id)
    )).scalars().all()
    assert len(eventi) == 1


@pytest.mark.asyncio
async def test_process_row_non_associato_sempre_inserito(db_session):
    """Righe senza match sono diagnostica: si inseriscono comunque
    (contact_id=NULL), senza dedup -- basso volume, SDD 7.3."""
    from app.services.wa_reply_watcher import process_chat_row
    from app.models.wa import WaInboundEvent
    from tests.factories_wa import make_number

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)

    esito = await process_chat_row(db_session, tenant_id=tenant.id,
                                   wa_number_id=numero.id,
                                   row=_row("Sconosciuto", preview="ciao"))
    assert esito["esito"] == "non_associato"

    eventi = (await db_session.execute(
        select(WaInboundEvent).where(WaInboundEvent.wa_number_id == numero.id)
    )).scalars().all()
    assert len(eventi) == 1
    assert eventi[0].contact_id is None


@pytest.mark.asyncio
async def test_process_row_replied_emette_evento(db_session, monkeypatch):
    from app.services import wa_reply_watcher
    from app.models.wa import WaContactStatus
    from tests.factories_wa import make_campaign, make_campaign_contact, make_number

    emessi = []
    monkeypatch.setattr(wa_reply_watcher.events, "emit",
                        lambda *a, **k: emessi.append((a, k)))

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    contatto.chat_title = "Marco"
    campagna, step = await make_campaign(db_session, tenant, numero)
    await make_campaign_contact(db_session, campagna, contatto,
                                status=WaContactStatus.in_sequence)
    await db_session.commit()

    await wa_reply_watcher.process_chat_row(
        db_session, tenant_id=tenant.id, wa_number_id=numero.id,
        row=_row("Marco", preview="ok grazie"))
    assert len(emessi) == 1
    assert emessi[0][0][1] == "wa.reply.received"
