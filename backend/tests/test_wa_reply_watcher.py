import pytest

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
