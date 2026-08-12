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
async def test_match_per_numero_trova_anche_i_contatti_scritti_dalla_fase_a(db_session):
    """Il secondo difetto del 12/08, e quello che ha reso cieco il gate
    opt-out sul 62% della campagna.

    Watcher e Fase A pseudonimizzano DUE stringhe diverse:
      wa_reply_watcher.py  cerca   hmac_phone("+" + cifre)
      wa_discover/salvataggio.py  scrive  hmac_phone(riga.numero)
    e `riga.numero` e' l'output nudo di normalize_e164, che ritorna le cifre
    SENZA il '+'. Il '+' non viene mai ricomposto, cosi' le due meta' non si
    incontrano mai: su 262 eventi inbound, matched_by='phone' e' rimasto 0.

    Misurato su produzione: 246 contatti su 258 sono nella forma della Fase A,
    e 154 hanno chat_title NULL (voluto: la promozione scarta le etichette
    mascherate). Per quelli il ramo phone e' l'UNICO modo di essere
    riconosciuti -- rotto lui, sono invisibili al reply-watcher.

    Finche' i dati storici convivono nelle due forme, la lettura deve
    accettarle entrambe. La scrittura si unifica con la migrazione, non prima:
    farlo adesso significherebbe che la Fase A non ritrova piu' le righe gia'
    a DB e ne inserisce di nuove, moltiplicando i duplicati."""
    from app.utils.phone_pseudonym import hmac_phone
    from app.services.wa_reply_watcher import match_contact

    tenant = await make_tenant(db_session)
    contatto = await make_contact(db_session, tenant, e164="+393331234567")
    contatto.phone_hmac = hmac_phone("393331234567")  # come scrive la Fase A
    await db_session.commit()

    row = _row("+39 333 1234567", title_is_number=True)
    trovato, via = await match_contact(db_session, tenant.id, row)
    assert trovato is not None, "contatto della Fase A invisibile al watcher"
    assert trovato.id == contatto.id
    assert via == WaMatchedBy.phone


@pytest.mark.asyncio
async def test_fra_due_contatti_con_lo_stesso_numero_vince_quello_arruolato(db_session):
    """Il rischio che il fix sopra introduce, se lo si scrive distratti.

    `phone_hmac` e' UNIQUE, ma le due forme non sono uguali per il DB: su
    produzione 9 numeri hanno gia' DUE WaContact distinti. Cercandole
    entrambe la query puo' tornare due righe, e uno `scalar()` senza ORDER BY
    ne prende una a caso.

    Se pesca il gemello sbagliato -- quello non arruolato -- persist_wa_optout
    marca il contatto che non riceve nulla: l'opt-out risulta registrato,
    l'evento risulta matched_by=phone, e la riga `queued` della campagna NON
    si ferma. Un fallimento indistinguibile dal successo, proprio sul gate
    che deve essere il piu' affidabile. Su produzione riguarda 6 numeri
    ancora `queued`.

    Vince quindi il contatto che ha una riga in wa_campaign_contacts: e' quello
    a cui stiamo scrivendo, ed e' l'unico che l'opt-out deve poter fermare."""
    from app.utils.phone_pseudonym import hmac_phone
    from app.services.wa_reply_watcher import match_contact
    from app.models.wa import WaCampaignStatus, WaContactStatus
    from tests.factories_wa import (make_campaign, make_campaign_contact,
                                    make_number)

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)

    # Il gemello nato da un onboarding manuale: forma con '+', mai arruolato.
    orfano = await make_contact(db_session, tenant, e164="+393331234567",
                                display_name="prova onboarding")

    # Quello vero della campagna: forma della Fase A, arruolato e in attesa.
    arruolato = await make_contact(db_session, tenant, e164="+393339999999",
                                   display_name="cliente Primero")
    arruolato.phone_hmac = hmac_phone("393331234567")
    campagna, _ = await make_campaign(db_session, tenant, numero,
                                      status=WaCampaignStatus.running)
    await make_campaign_contact(db_session, campagna, arruolato,
                                status=WaContactStatus.queued)
    await db_session.commit()

    row = _row("+39 333 1234567", title_is_number=True)
    trovato, via = await match_contact(db_session, tenant.id, row)
    assert via == WaMatchedBy.phone
    assert trovato.id == arruolato.id, (
        "ha pescato il gemello non arruolato: l'opt-out si sarebbe registrato "
        "sul contatto sbagliato e la campagna non si sarebbe fermata")
    assert trovato.id != orfano.id


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


def test_nfc_e_nfd_sono_byte_diversi_a_parita_di_testo_visivo():
    """Prova pura, senza DB: la stessa parola "citta'" con la lettera
    accentata puo' arrivare in due forme di normalizzazione Unicode diverse
    -- NFC (un solo code point per 'a') e NFD (lettera base + accento
    combinante separato). Sono la stessa cosa per un umano, ma '==' su
    stringhe Python le tratta come diverse: e' esattamente il confronto che
    match_contact fa su chat_title."""
    import unicodedata

    nfc = unicodedata.normalize("NFC", "città")
    nfd = unicodedata.normalize("NFD", "città")

    assert nfc != nfd                     # stesso testo, byte diversi
    assert len(nfd) > len(nfc)             # NFD ha un code point in piu' (l'accento separato)
    assert unicodedata.normalize("NFC", nfd) == nfc   # normalizzare le riallinea


@pytest.mark.asyncio
async def test_match_per_chat_title_matcha_anche_se_il_dom_arriva_in_nfd(db_session):
    """Backlog M4: chat_title a DB salvato in NFC (nome con lettere
    accentate, comune nei nomi italiani), il DOM di WhatsApp lo restituisce
    in NFD. Senza normalizzazione il confronto '==' fallisce in silenzio e
    il matching si perde -- niente crash, solo una risposta mai agganciata."""
    import unicodedata
    from app.services.wa_reply_watcher import match_contact

    tenant = await make_tenant(db_session)
    contatto = await make_contact(db_session, tenant, display_name="Citta' Bella")
    contatto.chat_title = unicodedata.normalize("NFC", "Città Bella")
    await db_session.commit()

    title_nfd = unicodedata.normalize("NFD", "Città Bella")
    assert title_nfd != contatto.chat_title   # precondizione: le due forme sono davvero diverse

    trovato, via = await match_contact(db_session, tenant.id, _row(title_nfd))
    assert trovato is not None
    assert trovato.id == contatto.id
    assert via == WaMatchedBy.chat_title


@pytest.mark.asyncio
async def test_ambiguita_nfc_nfd_cross_contatto_resta_rilevata(db_session):
    """Rischio specifico del fix .in_({nfc, nfd}): con un confronto singolo
    '==' due contatti con chat_title byte-diversi (uno NFC, l'altro NFD)
    non sarebbero MAI stati confusi. Ora la query cerca due varianti insieme
    -- se contatto A ha chat_title salvato in NFC e contatto B (stesso
    tenant) ha per coincidenza un chat_title che e' esattamente la forma NFD
    generata dallo stesso titolo in ingresso, la query trova ENTRAMBI.
    Verifichiamo che questo produca conteggio=2 -> ambiguo -> nessun match
    (mai indovinare, SDD 7.3), non un match a caso sul primo trovato."""
    import unicodedata
    from app.services.wa_reply_watcher import match_contact

    tenant = await make_tenant(db_session)
    nfc = unicodedata.normalize("NFC", "Città Bella")
    nfd = unicodedata.normalize("NFD", "Città Bella")
    assert nfc != nfd  # precondizione: le due forme sono davvero byte-diverse

    contatto_a = await make_contact(db_session, tenant, e164="+393330000011")
    contatto_a.chat_title = nfc
    contatto_b = await make_contact(db_session, tenant, e164="+393330000012")
    contatto_b.chat_title = nfd
    await db_session.commit()

    # Il DOM manda il titolo in una qualunque delle due forme: entrambe
    # generano lo stesso insieme di varianti {nfc, nfd}, quindi il risultato
    # non deve dipendere da quale delle due arriva.
    trovato_da_nfc, via_da_nfc = await match_contact(db_session, tenant.id, _row(nfc))
    trovato_da_nfd, via_da_nfd = await match_contact(db_session, tenant.id, _row(nfd))

    assert trovato_da_nfc is None
    assert via_da_nfc == WaMatchedBy.none
    assert trovato_da_nfd is None
    assert via_da_nfd == WaMatchedBy.none


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
async def test_process_row_basta_ambigua_lunga_non_fa_optout_ma_alerta(db_session, monkeypatch):
    """Review G6 (07/08): 'basta' in una frase lunga non e' un opt-out
    automatico -- la sequenza si ferma comunque (una risposta qualsiasi la
    ferma), ma il contatto NON va marcato opted_out/do_not_contact, e deve
    partire un alert per la revisione umana."""
    from app.services.wa_reply_watcher import process_chat_row
    from app.models.wa import WaCampaignContact, WaContactStatus
    from app.services import notifier
    from tests.factories_wa import make_campaign, make_campaign_contact, make_number

    chiamate = []

    async def _spy(msg, **kw):
        chiamate.append(msg)

    monkeypatch.setattr(notifier, "send_telegram", _spy)

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
        row=_row("Marco", preview="mi basta sapere se siete aperti"))
    assert esito["esito"] == "replied"

    await db_session.refresh(contatto)
    await db_session.refresh(cc)
    assert contatto.opted_out is False
    assert cc.status == WaContactStatus.replied
    assert len(chiamate) == 1
    assert "basta" in chiamate[0].lower()


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
    # Nessun testo archiviato: su un numero in coesistenza questa riga puo'
    # essere una chat personale del cliente, e non serve a nessuna decisione.
    assert eventi[0].preview_text is None


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
    # current_step=0 esplicito: `in_sequence` significa che uno step E' stato
    # inviato, e il default della factory e' -1 (stato di arruolamento). Il
    # gate su current_step >= 0 rifiuta la combinazione impossibile
    # in_sequence + mai inviato -- vedi
    # test_riga_completed_ma_mai_inviata_non_diventa_replied.
    await make_campaign_contact(db_session, campagna, contatto,
                                status=WaContactStatus.in_sequence, current_step=0)
    await db_session.commit()

    await wa_reply_watcher.process_chat_row(
        db_session, tenant_id=tenant.id, wa_number_id=numero.id,
        row=_row("Marco", preview="ok grazie"))
    assert len(emessi) == 1
    assert emessi[0][0][1] == "wa.reply.received"


async def _messaggio_inviato(db, campagna, contatto, numero, *, giorni_fa: float):
    """Una riga wa_messages 'sent' con sent_at nel passato -- e' il segnale su
    cui numeri_da_scansionare tiene vivo un numero dopo la fine degli invii."""
    import uuid
    from datetime import datetime, timedelta
    from app.models.wa import WaMessage, WaMessageStatus

    msg = WaMessage(id=str(uuid.uuid4()), campaign_id=campagna.id,
                    contact_id=contatto.id, wa_number_id=numero.id, step_index=0,
                    template_variant="a", rendered_text="Ciao",
                    status=WaMessageStatus.sent,
                    sent_at=datetime.utcnow() - timedelta(days=giorni_fa))
    db.add(msg)
    await db.flush()
    return msg


@pytest.mark.asyncio
async def test_contatto_completed_che_risponde_diventa_replied(db_session, monkeypatch):
    """Il caso NORMALE dell'MVP: campagne a un solo step (SDD Q29), quindi a
    invio finito il contatto e' 'completed' e la risposta arriva dopo. Con il
    watcher che cercava solo 'in_sequence' questo ramo era irraggiungibile:
    nessun replied, nessun evento, nessun contatore."""
    from app.services import wa_reply_watcher
    from app.models.wa import WaCampaignStatus, WaContactStatus
    from tests.factories_wa import make_campaign, make_campaign_contact, make_number

    emessi = []
    monkeypatch.setattr(wa_reply_watcher.events, "emit",
                        lambda *a, **k: emessi.append((a, k)))

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    contatto.chat_title = "Marco"
    campagna, _ = await make_campaign(db_session, tenant, numero,
                                      status=WaCampaignStatus.running)
    cc = await make_campaign_contact(db_session, campagna, contatto,
                                     status=WaContactStatus.completed, current_step=0)
    await db_session.commit()

    esito = await wa_reply_watcher.process_chat_row(
        db_session, tenant_id=tenant.id, wa_number_id=numero.id,
        row=_row("Marco", preview="si mi interessa, mi richiami?"))
    assert esito["esito"] == "replied"

    await db_session.refresh(cc)
    await db_session.refresh(campagna)
    assert cc.status == WaContactStatus.replied
    assert cc.replied_at_step == 0
    assert campagna.replied == 1
    assert [a[1] for a, _ in emessi] == ["wa.reply.received"]


@pytest.mark.asyncio
async def test_chi_non_ha_ancora_ricevuto_il_messaggio_non_diventa_replied(db_session):
    """Un messaggio in arrivo PRIMA del nostro invio non e' una risposta.

    `_campagna_attiva_del_contatto` includeva `queued` di proposito ("copre il
    caso simmetrico raro: risposta arrivata prima che partisse il nostro
    invio"). Ma `replied` e' TERMINALE: quel contatto non riceve mai il
    messaggio della campagna. Non e' una statistica sbagliata, e' un contatto
    scartato in silenzio senza essere mai stato contattato.

    Misurato dal vivo il 12/08 sulla campagna dei 246 Primero: 2 righe su 3 in
    `replied` avevano `current_step=-1` e ZERO messaggi inviati -- erano
    conversazioni gia' in corso col cliente, che il watcher ha letto come
    risposte. Su un numero con molte chat vive la perdita e' silenziosa e
    grande.
    """
    from app.services.wa_reply_watcher import process_chat_row
    from app.models.wa import WaCampaignStatus, WaContactStatus
    from tests.factories_wa import make_campaign, make_campaign_contact, make_number

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    contatto.chat_title = "Alessio Tutti A Tavola"
    campagna, _ = await make_campaign(db_session, tenant, numero,
                                      status=WaCampaignStatus.running)
    cc = await make_campaign_contact(db_session, campagna, contatto,
                                     status=WaContactStatus.queued, current_step=-1)
    await db_session.commit()

    esito = await process_chat_row(
        db_session, tenant_id=tenant.id, wa_number_id=numero.id,
        row=_row("Alessio Tutti A Tavola", preview="allora ci vediamo domani"))

    assert esito["esito"] == "ignorato", (
        "un messaggio arrivato prima del nostro invio non e' una risposta")
    await db_session.refresh(cc)
    await db_session.refresh(campagna)
    assert cc.status == WaContactStatus.queued, "la riga deve restare inviabile"
    assert cc.next_action_at is not None, "una riga non terminale ha un appuntamento"
    assert campagna.replied == 0


@pytest.mark.asyncio
async def test_riga_completed_ma_mai_inviata_non_diventa_replied(db_session):
    """Lo stato da solo non basta: serve current_step >= 0.

    `current_step` vale -1 all'arruolamento e passa a 0 col primo invio: e' il
    marcatore di "ha ricevuto". Una riga chiusa senza aver mai inviato (skip poi
    chiusura) non deve poter diventare replied solo perche' lo stato lo
    consentirebbe.
    """
    from app.services.wa_reply_watcher import process_chat_row
    from app.models.wa import WaCampaignStatus, WaContactStatus
    from tests.factories_wa import make_campaign, make_campaign_contact, make_number

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    contatto.chat_title = "Marco"
    campagna, _ = await make_campaign(db_session, tenant, numero,
                                      status=WaCampaignStatus.running)
    cc = await make_campaign_contact(db_session, campagna, contatto,
                                     status=WaContactStatus.completed, current_step=-1)
    await db_session.commit()

    esito = await process_chat_row(
        db_session, tenant_id=tenant.id, wa_number_id=numero.id,
        row=_row("Marco", preview="ciao tutto bene?"))

    assert esito["esito"] == "ignorato"
    await db_session.refresh(cc)
    assert cc.status == WaContactStatus.completed


@pytest.mark.asyncio
async def test_uno_stop_prima_dell_invio_resta_onorato(db_session):
    """Non-regressione del fix sopra: il gate vale SOLO per `replied`.

    Un opt-out non dipende dall'aver ricevuto qualcosa da noi -- se il cliente
    scrive STOP a un numero con cui era gia' in conversazione, quel STOP vale, e
    il contatto non va contattato. Stringere anche questo ramo significherebbe
    mandare marketing a chi ha detto di no.
    """
    from app.services.wa_reply_watcher import process_chat_row
    from app.models.wa import WaCampaignStatus, WaContactStatus
    from tests.factories_wa import make_campaign, make_campaign_contact, make_number

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    contatto.chat_title = "Marco"
    campagna, _ = await make_campaign(db_session, tenant, numero,
                                      status=WaCampaignStatus.running)
    await make_campaign_contact(db_session, campagna, contatto,
                                status=WaContactStatus.queued, current_step=-1)
    await db_session.commit()

    esito = await process_chat_row(
        db_session, tenant_id=tenant.id, wa_number_id=numero.id,
        row=_row("Marco", preview="STOP"))

    assert esito["esito"] == "optout"
    await db_session.refresh(contatto)
    await db_session.refresh(campagna)
    assert contatto.opted_out is True
    assert contatto.do_not_contact is True
    assert campagna.opted_out == 1, "l'opt-out va contato sulla campagna anche pre-invio"


@pytest.mark.asyncio
async def test_optout_incrementa_il_contatore_ed_emette_evento(db_session, monkeypatch):
    """Il ramo opt-out deve fare le stesse due cose del ramo replied: contatore
    di campagna e evento. L'evento richiede un campaign_id, che prima di questa
    fix il watcher non trovava quasi mai (cercava solo 'in_sequence')."""
    from app.services import wa_reply_watcher
    from app.models.wa import WaCampaignStatus, WaContactStatus
    from tests.factories_wa import make_campaign, make_campaign_contact, make_number

    emessi = []
    monkeypatch.setattr(wa_reply_watcher.wa_optout.events, "emit",
                        lambda *a, **k: emessi.append((a, k)))

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    contatto.chat_title = "Marco"
    campagna, _ = await make_campaign(db_session, tenant, numero,
                                      status=WaCampaignStatus.running)
    await make_campaign_contact(db_session, campagna, contatto,
                                status=WaContactStatus.completed, current_step=0)
    await db_session.commit()

    esito = await wa_reply_watcher.process_chat_row(
        db_session, tenant_id=tenant.id, wa_number_id=numero.id,
        row=_row("Marco", preview="basta non scrivermi piu'"))
    assert esito["esito"] == "optout"

    await db_session.refresh(campagna)
    assert campagna.opted_out == 1
    assert [a[1] for a, _ in emessi] == ["wa.optout"]


@pytest.mark.asyncio
async def test_optout_ripetuto_non_reincrementa_il_contatore(db_session, monkeypatch):
    """N1 (final review): la riga campagna resta 'completed' (stato terminale,
    persist_wa_optout non la flippa) quindi _campagna_attiva_del_contatto la
    ritrova per sempre. Una seconda STOP con preview DIVERSA dalla prima
    scavalca il dedup (che confronta solo l'ultimo evento salvato) e non deve
    ricontare il contatto gia' opted_out."""
    from app.services import wa_reply_watcher
    from app.models.wa import WaCampaignStatus, WaContactStatus
    from tests.factories_wa import make_campaign, make_campaign_contact, make_number

    _lock_profilo_libero(monkeypatch)

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    contatto.chat_title = "Marco"
    campagna, _ = await make_campaign(db_session, tenant, numero,
                                      status=WaCampaignStatus.running)
    await make_campaign_contact(db_session, campagna, contatto,
                                status=WaContactStatus.completed, current_step=0)
    await db_session.commit()

    primo = await wa_reply_watcher.process_chat_row(
        db_session, tenant_id=tenant.id, wa_number_id=numero.id,
        row=_row("Marco", preview="stop"))
    assert primo["esito"] == "optout"

    secondo = await wa_reply_watcher.process_chat_row(
        db_session, tenant_id=tenant.id, wa_number_id=numero.id,
        row=_row("Marco", preview="ho detto stop, basta"))
    assert secondo["esito"] == "optout"

    await db_session.refresh(campagna)
    assert campagna.opted_out == 1

    await db_session.refresh(contatto)
    assert contatto.opted_out is True
    assert contatto.do_not_contact is True


@pytest.mark.asyncio
async def test_numero_resta_scansionabile_dopo_la_fine_degli_invii(db_session):
    """Contatti tutti 'completed' ma invio recente: il numero deve restare in
    lista, altrimenti la scansione si spegne proprio quando le risposte
    iniziano ad arrivare."""
    from app.services.wa_reply_watcher import numeri_da_scansionare
    from app.models.wa import WaCampaignStatus, WaContactStatus
    from tests.factories_wa import make_campaign, make_campaign_contact, make_number

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant, label="Invio finito ieri")
    contatto = await make_contact(db_session, tenant)
    campagna, _ = await make_campaign(db_session, tenant, numero,
                                      status=WaCampaignStatus.running)
    await make_campaign_contact(db_session, campagna, contatto,
                                status=WaContactStatus.completed)
    await _messaggio_inviato(db_session, campagna, contatto, numero, giorni_fa=1)
    await db_session.commit()

    assert numero.id in await numeri_da_scansionare(db_session)


@pytest.mark.asyncio
async def test_numero_esce_dalla_lista_oltre_la_finestra(db_session, monkeypatch):
    """La finestra e' delimitata di proposito: oltre, un numero senza lavoro
    vivo esce -- includere i 'completed' per sempre farebbe crescere la lista
    di scansione senza controllo."""
    from app.config import settings
    from app.services.wa_reply_watcher import numeri_da_scansionare
    from app.models.wa import WaCampaignStatus, WaContactStatus
    from tests.factories_wa import make_campaign, make_campaign_contact, make_number

    monkeypatch.setattr(settings, "wa_reply_scan_window_days", 3)

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant, label="Invio di due settimane fa")
    contatto = await make_contact(db_session, tenant)
    campagna, _ = await make_campaign(db_session, tenant, numero,
                                      status=WaCampaignStatus.running)
    await make_campaign_contact(db_session, campagna, contatto,
                                status=WaContactStatus.completed)
    await _messaggio_inviato(db_session, campagna, contatto, numero, giorni_fa=14)
    await db_session.commit()

    assert numero.id not in await numeri_da_scansionare(db_session)


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

    riga_letta_e_nostra = _row("Altro", preview="test", unread=0)
    # Letta E con l'ultimo messaggio nostro: e' l'unica combinazione che si
    # salta. Una chat letta il cui ultimo messaggio e' del cliente va invece
    # processata (test_stop_su_chat_gia_letta_produce_comunque_optout).
    riga_letta_e_nostra.last_is_outbound = True

    righe_finte = [
        _row("Marco", preview="ciao", unread=1),
        riga_letta_e_nostra,
    ]

    class _PomFinto:
        def __init__(self, page):
            pass

        async def session_state(self):
            return "logged_in"

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
async def test_scan_number_sessione_non_pronta_non_tenta_lo_scan(db_session, monkeypatch):
    """Trovato provando dal vivo (QA Fase 4, 04/08): appena aperta, WhatsApp
    Web e' una SPA pesante -- la sidebar non e' garantita pronta subito dopo
    `page.goto(wait_until="domcontentloaded")`. `session_state()` (M1) ASPETTA
    fino a 90s la comparsa di CHATLIST; `scan_chat_list()` (M1) NON aspetta,
    valuta il DOM cosi' com'e' e solleva RuntimeError se la sidebar non c'e'
    ancora. scan_number deve verificare lo stato PRIMA di scansionare, non
    lasciar propagare un RuntimeError per un semplice ritardo di caricamento."""
    from app.services import wa_reply_watcher
    from tests.factories_wa import make_number

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    await db_session.commit()

    class _PomFinto:
        def __init__(self, page):
            pass

        async def session_state(self):
            return "qr_required"

        async def scan_chat_list(self):
            raise AssertionError("scan_chat_list non deve partire se la sessione non e' pronta")

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
    assert esito["motivo"] == "sessione_non_pronta"
    assert esito["scansionate"] == 0


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
        async def session_state(self):
            return "logged_in"
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


def _monta_browser_finto(monkeypatch, righe):
    """Le tre finzioni che servono a far girare scan_number senza browser:
    POM, context di Playwright, lucchetto profilo. Estratto qui perche' i
    test sull'unread ne hanno bisogno uguale ai precedenti."""
    from app.services import wa_reply_watcher

    class _PomFinto:
        def __init__(self, page):
            pass

        async def session_state(self):
            return "logged_in"

        async def scan_chat_list(self):
            return righe

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


@pytest.mark.asyncio
async def test_stop_su_chat_gia_letta_produce_comunque_optout(db_session, monkeypatch):
    """Il difetto del 12/08, misurato dal vivo: un cliente ha risposto STOP e
    il watcher non l'ha registrato (0 opt-out su 53 messaggi inviati).

    La sessione di invio DEVE aprire la chat per scrivere, e la lascia aperta
    uscendo. Se lo STOP arriva mentre quella chat e' l'attiva a schermo,
    WhatsApp Web la marca letta all'istante: `unread_count` torna a 0 e il
    filtro del chiamante saltava la riga prima di guardarne la preview. Nel PoC
    il browser non inviava, quindi non apriva chat e la condizione non poteva
    presentarsi -- per questo "funzionava nel PoC".

    Il segnale corretto non e' l'unread ma la DIREZIONE dell'ultimo messaggio:
    la preview della sidebar c'e' anche per le chat lette, e non serve aprire
    nulla per leggerla (il vincolo di coesistenza resta rispettato)."""
    from app.services import wa_reply_watcher
    from app.models.wa import WaCampaignStatus, WaContactStatus
    from tests.factories_wa import make_campaign, make_campaign_contact, make_number

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    contatto.chat_title = "Marco"
    campagna, _ = await make_campaign(db_session, tenant, numero,
                                      status=WaCampaignStatus.running)
    cc = await make_campaign_contact(db_session, campagna, contatto,
                                     status=WaContactStatus.completed, current_step=0)
    await db_session.commit()

    riga = _row("Marco", preview="stop non scrivermi piu'", unread=0)
    riga.last_is_outbound = False  # l'ultimo messaggio e' del cliente
    _monta_browser_finto(monkeypatch, [riga])

    esito = await wa_reply_watcher.scan_number(numero.id)
    assert esito["optout"] == 1

    await db_session.refresh(contatto)
    await db_session.refresh(cc)
    await db_session.refresh(campagna)
    assert contatto.opted_out is True
    assert contatto.do_not_contact is True
    # `completed` e' terminale e resta com'e': le campagne MVP sono a un solo
    # step, quindi lo STOP arriva quasi sempre a sequenza gia' chiusa e li' non
    # c'e' nulla da fermare. La protezione vera e' `do_not_contact` sul
    # contatto, che lo tiene fuori da OGNI campagna futura -- ed e' proprio la
    # riga che il 12/08 non e' mai stata scritta.
    assert cc.status == WaContactStatus.completed
    assert campagna.opted_out == 1


@pytest.mark.asyncio
async def test_chat_letta_con_ultimo_messaggio_nostro_resta_ignorata(db_session, monkeypatch):
    """Il contrappeso al test sopra, e il motivo per cui il filtro unread non
    si puo' semplicemente togliere.

    Su una chat letta la preview e' l'ultimo messaggio, chiunque l'abbia
    scritto. Se e' il NOSTRO, processare la riga marcherebbe `replied` un
    contatto che non ha mai risposto -- e `replied` e' terminale: quel contatto
    esce dalla campagna. E' lo stesso difetto gia' pagato il 12/08 (2 righe su
    3 in `replied` avevano zero messaggi inviati), preso dall'altro verso."""
    from app.services import wa_reply_watcher
    from app.models.wa import WaCampaignStatus, WaContactStatus
    from tests.factories_wa import make_campaign, make_campaign_contact, make_number

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    contatto.chat_title = "Marco"
    campagna, _ = await make_campaign(db_session, tenant, numero,
                                      status=WaCampaignStatus.running)
    cc = await make_campaign_contact(db_session, campagna, contatto,
                                     status=WaContactStatus.completed, current_step=0)
    await db_session.commit()

    riga = _row("Marco", preview="Buongiorno, le scrivo da Primero...", unread=0)
    riga.last_is_outbound = True  # l'ultimo messaggio e' il nostro
    _monta_browser_finto(monkeypatch, [riga])

    esito = await wa_reply_watcher.scan_number(numero.id)
    assert esito["scansionate"] == 0

    await db_session.refresh(cc)
    assert cc.status == WaContactStatus.completed
