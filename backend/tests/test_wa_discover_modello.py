"""Il modello di staging della Fase A auto-discover.

Perche' una tabella nuova invece di riusare wa_contacts: WaContact ha
encrypted_phone e phone_hmac NOT NULL (app/models/wa.py), quindi una chat di cui
non si legge il numero -- il 100% dei gruppi e una parte delle 1:1, misurato nel
PoC-4 -- non potrebbe proprio esistere li'. Il primo test di questo file e'
esattamente quella differenza.
"""
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError


async def _scoperte_di(db_session, number_id):
    """Le scoperte di QUESTO numero.

    Mai `select(WaDiscoveredChat)` nudo seguito da `scalar_one()`: il
    `db_session` del conftest promette isolamento ("rollback a fine test") ma il
    rollback arriva DOPO il commit, che ha gia' persistito -- quindi ogni test
    che committa lascia le sue righe agli altri. Un test che interroga la
    tabella intera passa da solo e fallisce in suite, che e' il modo piu' lento
    possibile di scoprire un problema che non esiste nel prodotto.
    """
    from app.models.wa import WaDiscoveredChat

    return (await db_session.execute(
        select(WaDiscoveredChat).where(WaDiscoveredChat.number_id == number_id)
    )).scalars().all()


@pytest_asyncio.fixture
async def numero_wa(db_session):
    """Un tenant e un numero WhatsApp veri a cui agganciare le scoperte."""
    from app.models.tenant import Tenant
    from app.models.wa import WaNumber

    tenant = Tenant(id=str(uuid.uuid4()), name="T-discover", status="active")
    db_session.add(tenant)
    await db_session.flush()
    numero = WaNumber(
        id=str(uuid.uuid4()), tenant_id=tenant.id, label="numero-discover",
        phone_hmac=f"h-{uuid.uuid4().hex[:8]}", encrypted_phone="e1",
        daily_cap=100, warmup_day=0, sent_today=0, sent_date=None,
    )
    db_session.add(numero)
    await db_session.commit()
    return numero


@pytest.mark.asyncio
async def test_chat_senza_numero_e_salvabile(db_session, numero_wa):
    """La ragione d'essere della tabella: una chat di cui NON si legge il numero
    deve poter essere salvata comunque, marcata (spec 5.3/5.4). In wa_contacts
    sarebbe impossibile.
    """
    from app.models.wa import WaDiscoveredChat

    db_session.add(WaDiscoveredChat(
        tenant_id=numero_wa.tenant_id, number_id=numero_wa.id,
        chat_title="AZIENDA AGRICOLA PRIMERO",
        display_name="AZIENDA AGRICOLA PRIMERO",
        encrypted_phone=None, phone_hmac=None,
        numero_leggibile=False, tipo_chat="gruppo", status="nuovo",
    ))
    await db_session.commit()

    trovate = await _scoperte_di(db_session, numero_wa.id)
    assert len(trovate) == 1
    trovata = trovate[0]
    assert trovata.phone_hmac is None
    assert trovata.encrypted_phone is None
    assert trovata.numero_leggibile is False
    assert trovata.tipo_chat == "gruppo"


@pytest.mark.asyncio
async def test_il_tipo_chat_ha_un_terzo_stato(db_session, numero_wa):
    """'ignoto' deve essere rappresentabile, ed e' il DEFAULT.

    Il PoC-4 ha misurato recall 1/6 sul rilevamento gruppo e un 5% di pannelli
    che non si aprono nemmeno su chat 1:1 vere: "non lo so" e' uno stato
    frequente. Un booleano lo appiattirebbe su 'non e' un gruppo' oppure su
    'e' un gruppo', e in entrambi i casi il dubbio sparirebbe dai dati.
    """
    from app.models.wa import WaDiscoveredChat

    db_session.add(WaDiscoveredChat(
        tenant_id=numero_wa.tenant_id, number_id=numero_wa.id,
        chat_title="Fulvio", display_name="Fulvio",
    ))
    await db_session.commit()

    trovate = await _scoperte_di(db_session, numero_wa.id)
    assert len(trovate) == 1
    trovata = trovate[0]
    assert trovata.tipo_chat == "ignoto"
    assert trovata.status == "nuovo"
    assert trovata.numero_leggibile is False


@pytest.mark.asyncio
async def test_stessa_chat_sullo_stesso_numero_non_duplica(db_session, numero_wa):
    """Spec 5.3: ri-scansionare la stessa lista deve essere innocuo."""
    from app.models.wa import WaDiscoveredChat

    comune = dict(tenant_id=numero_wa.tenant_id, number_id=numero_wa.id,
                  chat_title="Fulvio", display_name="Fulvio",
                  encrypted_phone="x", phone_hmac="abc123",
                  numero_leggibile=True, tipo_chat="individuale", status="nuovo")
    db_session.add(WaDiscoveredChat(**comune))
    await db_session.commit()

    db_session.add(WaDiscoveredChat(**comune))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_la_stessa_persona_su_due_numeri_e_due_scoperte(db_session, numero_wa):
    """La unique e' su (number_id, chat_title), non sul solo titolo.

    Due numeri WhatsApp hanno rubriche diverse: 'Fulvio' nel telefono del
    negozio e 'Fulvio' nel telefono personale sono due chat distinte, e vanno
    scoperte due volte. Una unique sul solo titolo ne perderebbe una in
    silenzio.
    """
    from app.models.wa import WaDiscoveredChat, WaNumber

    secondo = WaNumber(
        id=str(uuid.uuid4()), tenant_id=numero_wa.tenant_id, label="secondo",
        phone_hmac=f"h-{uuid.uuid4().hex[:8]}", encrypted_phone="e2",
        daily_cap=100, warmup_day=0, sent_today=0, sent_date=None,
    )
    db_session.add(secondo)
    await db_session.flush()

    for number_id in (numero_wa.id, secondo.id):
        db_session.add(WaDiscoveredChat(
            tenant_id=numero_wa.tenant_id, number_id=number_id,
            chat_title="Fulvio", display_name="Fulvio",
        ))
    await db_session.commit()

    assert len(await _scoperte_di(db_session, numero_wa.id)) == 1
    assert len(await _scoperte_di(db_session, secondo.id)) == 1


@pytest.mark.asyncio
async def test_riscansione_non_duplica_nemmeno_le_chat_senza_titolo(db_session, numero_wa):
    """Il buco che la sola UNIQUE(number_id, chat_title) lascia aperto.

    In SQL NULL non e' uguale a NULL: due righe con chat_title NULL non violano
    quella unique, e passano entrambe. Ma chat_title e' NULL proprio nel caso
    piu' frequente -- quando il titolo E' il numero e quindi non si salva in
    chiaro (P12): il 39% delle chat di Primero. Senza una seconda unique sul
    numero, ri-scansionare duplicherebbe in silenzio due contatti su cinque, che
    e' l'esatto contrario dell'"innocuo" promesso dalla spec 5.3.
    """
    from app.models.wa import WaDiscoveredChat

    # Gli id si copiano PRIMA: dopo il rollback gli oggetti ORM sono scaduti, e
    # leggere `numero_wa.id` farebbe ripartire un caricamento sulla sessione
    # appena rollbackata -- l'errore che ne esce nasconde quello che il test
    # stava davvero verificando (stessa forma del "log difensivo che rompe la
    # difesa": un accesso ORM innocuo dopo un commit fallito).
    number_id, tenant_id = numero_wa.id, numero_wa.tenant_id

    comune = dict(tenant_id=tenant_id, number_id=number_id,
                  chat_title=None, display_name=None,
                  encrypted_phone="gAAAA-finto", phone_hmac="hmac-duplicato",
                  numero_leggibile=True, tipo_chat="individuale")
    db_session.add(WaDiscoveredChat(**comune))
    await db_session.commit()

    db_session.add(WaDiscoveredChat(**comune))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

    assert len(await _scoperte_di(db_session, number_id)) == 1


@pytest.mark.asyncio
async def test_titolo_nullo_per_le_chat_il_cui_titolo_e_il_numero(db_session, numero_wa):
    """P12. Il 39% delle chat di Primero ha il numero COME titolo (PoC-5): quel
    valore va cifrato, e chat_title resta vuoto. Il modello deve permetterlo --
    se chat_title fosse NOT NULL, la via piu' comoda diventerebbe scriverci
    dentro il numero in chiaro.
    """
    from app.models.wa import WaDiscoveredChat

    db_session.add(WaDiscoveredChat(
        tenant_id=numero_wa.tenant_id, number_id=numero_wa.id,
        chat_title=None, display_name=None,
        encrypted_phone="gAAAAA-finto", phone_hmac="hmacfinto",
        numero_leggibile=True, tipo_chat="individuale",
    ))
    await db_session.commit()

    trovate = await _scoperte_di(db_session, numero_wa.id)
    assert len(trovate) == 1
    trovata = trovate[0]
    assert trovata.chat_title is None
    assert trovata.numero_leggibile is True
