"""Test di backend/app/services/wa_promote/arruolamento.py (Fase B, Task 3).

Stessa seconda meta' di wa_ingest.ingerisci_csv (righe 176-199), isolata qui
perche' il contatto ESISTE gia' (niente CSV/normalizzazione) e la funzione
serve anche fuori dal flusso Fase B. Stile di test_wa_promote_promozione.py.

Due decisioni di design non esplicitate nel piano, coperte qui con un test
dedicato ciascuna (vedi arruolamento.py per il ragionamento):
  - campagna INESISTENTE (non solo non-draft) solleva CampagnaNonModificabile,
    non un AttributeError su None.status;
  - un contact_id di un tenant diverso da quello della campagna si scarta
    come 'contatto_inesistente' (stesso principio del gap IDOR gia' corretto
    in promozione.promuovi -- mai un motivo che distingua "non esiste" da
    "non e' tuo").
La savepoint di concorrenza e' anch'essa oltre la lista test del piano, ma
richiesta dai "Vincoli globali": "persistenza con lookup esplicita +
SAVEPOINT/IntegrityError come ripiego sulla concorrenza (mai un INSERT
lasciato parlare al vincolo)" vale per OGNI file nuovo, non solo per
promozione.py.
"""
import uuid

import pytest
from sqlalchemy import func, select

from app.models.wa import WaCampaignContact, WaCampaignStatus, WaContactStatus
from app.services.wa_promote import arruolamento
from app.services.wa_promote.arruolamento import CampagnaNonModificabile, Scarto
from tests.factories_wa import (make_campaign, make_campaign_contact, make_contact,
                                make_number, make_tenant)


async def _ctx(db, *, status=WaCampaignStatus.draft):
    tenant = await make_tenant(db)
    number = await make_number(db, tenant)
    campagna, _ = await make_campaign(db, tenant, number, status=status)
    await db.commit()
    return tenant, campagna


@pytest.mark.asyncio
async def test_arruola_in_campagna_draft(db_session):
    tenant, campagna = await _ctx(db_session)
    contatti = [await make_contact(db_session, tenant) for _ in range(3)]
    await db_session.commit()

    report = await arruolamento.arruola(
        db_session, campaign_id=campagna.id, contact_ids=[c.id for c in contatti])

    assert report.arruolati == 3
    assert report.gia_presenti == 0
    assert report.gia_dnc == 0
    assert report.scarti == []

    righe = (await db_session.execute(
        select(WaCampaignContact).where(WaCampaignContact.campaign_id == campagna.id)
    )).scalars().all()
    assert len(righe) == 3
    for cc in righe:
        # Contratto §7.2/I3: mai NULL su una riga non terminale, valorizzato
        # SUBITO dentro arruola(), non da un passo successivo.
        assert cc.next_action_at is not None
        assert cc.status == WaContactStatus.queued
        assert cc.current_step == -1
        assert cc.failure_count == 0
        assert cc.locked_by is None and cc.locked_at is None

    await db_session.refresh(campagna)
    assert campagna.total_contacts == 3


@pytest.mark.asyncio
async def test_campagna_non_draft_rifiutata(db_session):
    tenant, campagna = await _ctx(db_session, status=WaCampaignStatus.running)
    contatto = await make_contact(db_session, tenant)
    await db_session.commit()

    with pytest.raises(CampagnaNonModificabile):
        await arruolamento.arruola(db_session, campaign_id=campagna.id,
                                   contact_ids=[contatto.id])

    # Fail-fast: la campagna si verifica PRIMA di processare qualunque id,
    # niente deve essere stato scritto.
    righe = (await db_session.execute(
        select(WaCampaignContact).where(WaCampaignContact.campaign_id == campagna.id)
    )).scalars().all()
    assert righe == []


@pytest.mark.asyncio
async def test_campagna_inesistente_rifiutata(db_session):
    """Non solo non-draft: una campagna che non esiste proprio non e'
    modificabile a maggior ragione. Senza questo guard, campagna.status su
    None sarebbe un AttributeError (500 grezzo), non un errore chiaro."""
    with pytest.raises(CampagnaNonModificabile):
        await arruolamento.arruola(db_session, campaign_id=str(uuid.uuid4()),
                                   contact_ids=[str(uuid.uuid4())])


@pytest.mark.asyncio
async def test_contatto_opted_out_escluso(db_session):
    tenant, campagna = await _ctx(db_session)
    contatto = await make_contact(db_session, tenant)
    contatto.opted_out = True
    contatto.do_not_contact = True
    await db_session.commit()

    report = await arruolamento.arruola(db_session, campaign_id=campagna.id,
                                        contact_ids=[contatto.id])

    assert report.gia_dnc == 1
    assert report.arruolati == 0
    righe = (await db_session.execute(
        select(WaCampaignContact).where(WaCampaignContact.campaign_id == campagna.id)
    )).scalars().all()
    assert righe == []   # l'opt-out vince: nessuna riga creata


@pytest.mark.asyncio
async def test_gia_arruolato_non_duplica(db_session):
    tenant, campagna = await _ctx(db_session)
    contatto = await make_contact(db_session, tenant)
    await make_campaign_contact(db_session, campagna, contatto)
    await db_session.commit()

    report = await arruolamento.arruola(db_session, campaign_id=campagna.id,
                                        contact_ids=[contatto.id])

    assert report.gia_presenti == 1
    assert report.arruolati == 0
    n = await db_session.scalar(
        select(func.count(WaCampaignContact.id))
        .where(WaCampaignContact.campaign_id == campagna.id,
               WaCampaignContact.contact_id == contatto.id))
    assert n == 1   # non duplicato


@pytest.mark.asyncio
async def test_contatto_inesistente_si_scarta(db_session):
    tenant, campagna = await _ctx(db_session)
    id_sconosciuto = str(uuid.uuid4())

    report = await arruolamento.arruola(db_session, campaign_id=campagna.id,
                                        contact_ids=[id_sconosciuto])

    assert report.scarti == [Scarto(id=id_sconosciuto, motivo="contatto_inesistente")]
    assert report.arruolati == 0


@pytest.mark.asyncio
async def test_contact_id_con_null_byte_si_scarta_senza_500(db_session):
    """Stesso difetto trovato in QA su promozione.py: un contact_id con
    '\\x00' non deve arrivare al driver (asyncpg lo rifiuta con
    un'eccezione non catturata)."""
    tenant, campagna = await _ctx(db_session)

    report = await arruolamento.arruola(db_session, campaign_id=campagna.id,
                                        contact_ids=["abc\x00def"])

    assert report.scarti == [Scarto(id="abc\x00def", motivo="contatto_inesistente")]
    assert report.arruolati == 0


@pytest.mark.asyncio
async def test_campaign_id_con_null_byte_e_campagna_inesistente(db_session):
    """Stessa validazione vale per campaign_id: il caricamento fail-fast
    della campagna non deve nemmeno lui arrivare al driver con un id
    ostile."""
    with pytest.raises(CampagnaNonModificabile):
        await arruolamento.arruola(db_session, campaign_id="abc\x00def", contact_ids=[])


@pytest.mark.asyncio
async def test_contatto_di_altro_tenant_si_scarta_come_inesistente(db_session):
    """Stesso principio 'per costruzione' del gap IDOR gia' corretto in
    promozione.promuovi: un contact_id che esiste ma appartiene a un tenant
    diverso da quello della campagna si scarta con lo STESSO motivo di un id
    inesistente, mai un motivo diverso che farebbe trapelare 'esiste ma non
    e' tuo' a chi indovina id."""
    tenant, campagna = await _ctx(db_session)
    altro_tenant = await make_tenant(db_session, name="Altro")
    contatto_altrui = await make_contact(db_session, altro_tenant)
    await db_session.commit()

    report = await arruolamento.arruola(db_session, campaign_id=campagna.id,
                                        contact_ids=[contatto_altrui.id])

    assert report.scarti == [Scarto(id=contatto_altrui.id, motivo="contatto_inesistente")]
    assert report.arruolati == 0


@pytest.mark.asyncio
async def test_total_contacts_aggiornato_senza_read_modify_write(db_session):
    """Stesso vincolo di wa_contacts.rimuovi_contatto:139 (UPDATE diretto,
    mai un SELECT count(*)), qui in incremento e su piu' batch, compreso uno
    parziale (un id gia' presente + uno nuovo nello stesso batch)."""
    tenant, campagna = await _ctx(db_session)
    c1 = await make_contact(db_session, tenant)
    c2 = await make_contact(db_session, tenant)
    c3 = await make_contact(db_session, tenant)
    await db_session.commit()

    primo = await arruolamento.arruola(db_session, campaign_id=campagna.id,
                                       contact_ids=[c1.id, c2.id])
    assert primo.arruolati == 2
    await db_session.refresh(campagna)
    assert campagna.total_contacts == 2

    secondo = await arruolamento.arruola(db_session, campaign_id=campagna.id,
                                         contact_ids=[c1.id, c3.id])
    assert secondo.arruolati == 1
    assert secondo.gia_presenti == 1
    await db_session.refresh(campagna)
    assert campagna.total_contacts == 3   # +1, non ricalcolato da zero


@pytest.mark.asyncio
async def test_batch_interamente_senza_nuovi_arruolamenti_non_tocca_il_contatore(db_session):
    tenant, campagna = await _ctx(db_session)
    contatto = await make_contact(db_session, tenant)
    contatto.opted_out = True
    await db_session.commit()

    report = await arruolamento.arruola(db_session, campaign_id=campagna.id,
                                        contact_ids=[contatto.id])
    assert report.arruolati == 0
    await db_session.refresh(campagna)
    assert campagna.total_contacts == 0


@pytest.mark.asyncio
async def test_arruolamento_concorrente_stesso_contatto_non_duplica(db_session):
    """Vincolo globale del piano: SAVEPOINT/IntegrityError come ripiego
    sulla concorrenza, stesso schema di wa_ingest/promozione -- due
    arruolamenti concorrenti sullo stesso (campaign_id, contact_id) devono
    convergere senza IntegrityError non catturato, con UNA sola riga
    WaCampaignContact."""
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import settings
    from app.utils.db_dialect import to_async_database_url

    tenant, campagna = await _ctx(db_session)
    contatto = await make_contact(db_session, tenant)
    await db_session.commit()

    eng = create_async_engine(to_async_database_url(settings.database_url))
    Session = async_sessionmaker(eng, expire_on_commit=False)

    async def arruola_in_propria_sessione():
        async with Session() as db:
            return await arruolamento.arruola(db, campaign_id=campagna.id,
                                              contact_ids=[contatto.id])

    r_a, r_b = await asyncio.gather(
        arruola_in_propria_sessione(), arruola_in_propria_sessione())

    # Un vincitore la crea (arruolati=1), l'altro la vede gia' presente
    # (gia_presenti=1) -- nessuna eccezione risalita, nessuna riga persa.
    assert sorted([r_a.arruolati, r_b.arruolati]) == [0, 1]
    assert sorted([r_a.gia_presenti, r_b.gia_presenti]) == [0, 1]

    async with Session() as check_db:
        n = await check_db.scalar(
            select(func.count(WaCampaignContact.id))
            .where(WaCampaignContact.campaign_id == campagna.id,
                   WaCampaignContact.contact_id == contatto.id))
        assert n == 1   # dedup regge, una sola riga
    await eng.dispose()
