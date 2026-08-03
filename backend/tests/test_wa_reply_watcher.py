import contextlib

import pytest
from sqlalchemy import select

from app.browser.whatsapp_page import ChatRow
from app.models.wa import WaMatchedBy
from tests.factories_wa import make_contact, make_tenant


def _lock_profilo_libero(monkeypatch):
    """Lucchetto profilo no-op: `held` fa un `arq.create_pool` VERO e senza un
    demone Redis vivo costa ~50s di retry prima di fallire. La mutua esclusione
    vera si prova in test_wa_profile_lock.py (fixture _redis_o_skip)."""
    from app.services import wa_reply_watcher

    @contextlib.asynccontextmanager
    async def _libero(number_id, *, ttl_min=None):
        yield "token-di-test"
    monkeypatch.setattr(wa_reply_watcher.wa_profile_lock, "held", _libero)


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


@pytest.mark.asyncio
async def test_numeri_da_scansionare_solo_con_lavoro_vivo(db_session):
    from app.services.wa_reply_watcher import numeri_da_scansionare
    from app.models.wa import WaCampaignStatus, WaContactStatus
    from tests.factories_wa import make_campaign, make_campaign_contact, make_number

    tenant = await make_tenant(db_session)

    numero_vivo = await make_number(db_session, tenant, label="Vivo")
    contatto1 = await make_contact(db_session, tenant, e164="+393331111111")
    campagna1, _ = await make_campaign(db_session, tenant, numero_vivo,
                                       status=WaCampaignStatus.running)
    await make_campaign_contact(db_session, campagna1, contatto1,
                                status=WaContactStatus.in_sequence)

    numero_finito = await make_number(db_session, tenant, label="Finito")
    contatto2 = await make_contact(db_session, tenant, e164="+393332222222")
    campagna2, _ = await make_campaign(db_session, tenant, numero_finito,
                                       status=WaCampaignStatus.running)
    await make_campaign_contact(db_session, campagna2, contatto2,
                                status=WaContactStatus.completed)

    await db_session.commit()

    ids = await numeri_da_scansionare(db_session)
    assert numero_vivo.id in ids
    assert numero_finito.id not in ids


@pytest.mark.asyncio
async def test_scan_number_processa_le_righe_non_lette(db_session, monkeypatch):
    from app.services import wa_reply_watcher
    from app.database import AsyncSessionLocal
    from tests.factories_wa import make_number

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    contatto.chat_title = "Marco"
    await db_session.commit()

    righe_finte = [
        _row("Marco", preview="ciao", unread=1),
        _row("Altro", preview="test", unread=0),  # unread=0, va ignorata
    ]

    class _PomFinto:
        def __init__(self, page):
            pass

        async def scan_chat_list(self):
            return righe_finte

    monkeypatch.setattr(wa_reply_watcher, "WhatsAppWebPage", _PomFinto)

    class _ContextFinto:
        async def new_page(self):
            class _PageFinta:
                async def goto(self, *a, **k):
                    pass
            return _PageFinta()

    class _BrowserCtx:
        def __call__(self, number_id, headless=True, proxy_url=None):
            return self

        async def __aenter__(self):
            return _ContextFinto()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(wa_reply_watcher, "_open_wa_browser", _BrowserCtx())
    _lock_profilo_libero(monkeypatch)

    async def _mai_halted():
        return False
    monkeypatch.setattr(wa_reply_watcher.bot_state_service, "is_wa_halted", _mai_halted)

    esito = await wa_reply_watcher.scan_number(numero.id)
    assert esito["scansionate"] == 1  # solo la riga con unread>0


@pytest.mark.asyncio
async def test_e2e_optout_ferma_tutte_le_campagne_del_contatto(db_session, monkeypatch):
    """Scenario completo SDD §7.5: STOP su UNA campagna ferma TUTTE le righe
    non terminali del contatto, in QUALUNQUE campagna del tenant."""
    from app.services import wa_reply_watcher
    from app.models.wa import WaCampaignStatus, WaContactStatus
    from tests.factories_wa import make_campaign, make_campaign_contact, make_number

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    contatto.chat_title = "Marco"

    camp_a, _ = await make_campaign(db_session, tenant, numero, name="A",
                                    status=WaCampaignStatus.running)
    camp_b, _ = await make_campaign(db_session, tenant, numero, name="B",
                                    status=WaCampaignStatus.running)
    cc_a = await make_campaign_contact(db_session, camp_a, contatto,
                                       status=WaContactStatus.in_sequence)
    cc_b = await make_campaign_contact(db_session, camp_b, contatto,
                                       status=WaContactStatus.queued)
    await db_session.commit()

    righe_finte = [_row("Marco", preview="stop non scrivermi piu'", unread=1)]

    class _PomFinto:
        def __init__(self, page):
            pass
        async def scan_chat_list(self):
            return righe_finte

    monkeypatch.setattr(wa_reply_watcher, "WhatsAppWebPage", _PomFinto)

    class _ContextFinto:
        async def new_page(self):
            class _PageFinta:
                async def goto(self, *a, **k):
                    pass
            return _PageFinta()

    class _BrowserCtx:
        def __call__(self, number_id, headless=True, proxy_url=None):
            return self
        async def __aenter__(self):
            return _ContextFinto()
        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(wa_reply_watcher, "_open_wa_browser", _BrowserCtx())
    _lock_profilo_libero(monkeypatch)

    async def _mai_halted():
        return False
    monkeypatch.setattr(wa_reply_watcher.bot_state_service, "is_wa_halted", _mai_halted)

    esito = await wa_reply_watcher.scan_number(numero.id)
    assert esito["optout"] == 1

    await db_session.refresh(contatto)
    await db_session.refresh(cc_a)
    await db_session.refresh(cc_b)
    assert contatto.opted_out is True
    assert contatto.do_not_contact is True
    assert cc_a.status == WaContactStatus.opted_out
    assert cc_b.status == WaContactStatus.opted_out
