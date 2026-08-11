"""Test di backend/app/services/wa_promote/promozione.py (Fase B, Task 2).

Stile di `test_wa_ingest.py`: creazione, riuso (dedup), concorrenza -- qui in
piu' il caso specifico di Fase B, il gruppo iniettato a mano nel batch e
l'idempotenza della doppia promozione (invariante "status non torna mai
indietro", vincolo globale del piano).

`promuovi()` prende `tenant_id` OBBLIGATORIO (corretto in review: la bozza
iniziale ne era priva, un vero gap IDOR -- un id di `wa_discovered_chats` di
un altro tenant sarebbe stato trovato per id nudo e promosso comunque, sia
pure sotto il SUO tenant reale via `riga.tenant_id`, ma innescato da un
operatore che non aveva alcun diritto su quella riga). Stesso principio "per
costruzione" del vincolo sui gruppi (Task 1): la funzione non si fida di chi
la chiama, verifica da sola. Vedi `test_riga_di_altro_tenant_si_scarta`.
"""
import uuid

import pytest
from sqlalchemy import func, select

from app.models.wa import WaContact
from app.services.wa_promote import promozione
from app.services.wa_promote.promozione import Scarto
from tests.factories_wa import (make_contact, make_discovered_chat, make_number,
                                make_tenant)


async def _ctx(db):
    tenant = await make_tenant(db)
    number = await make_number(db, tenant)
    await db.commit()
    return tenant, number


@pytest.mark.asyncio
async def test_crea_wacontact_e_segna_promosso(db_session):
    tenant, number = await _ctx(db_session)
    riga = await make_discovered_chat(db_session, tenant, number,
                                      chat_title="Mario Rossi", display_name="Mario Rossi")
    await db_session.commit()

    report = await promozione.promuovi(db_session, tenant_id=tenant.id, ids=[riga.id])

    assert report.promossi == 1
    assert report.contatti_creati == 1
    assert report.contatti_riusati == 0
    assert report.scarti == []
    assert report.contatti_promossi_ids

    contatto = await db_session.scalar(
        select(WaContact).where(WaContact.tenant_id == tenant.id,
                                WaContact.phone_hmac == riga.phone_hmac))
    assert contatto is not None
    assert contatto.id == report.contatti_promossi_ids[0]
    assert contatto.chat_title == "Mario Rossi"

    riga_ricaricata = await db_session.get(type(riga), riga.id)
    assert riga_ricaricata.status == "promosso"


@pytest.mark.asyncio
async def test_riusa_wacontact_esistente_stesso_hmac(db_session):
    """La unique e' (tenant_id, phone_hmac), stessa di Fase A: un WaContact
    gia' presente per lo stesso numero si riusa, non se ne crea un secondo."""
    tenant, number = await _ctx(db_session)
    e164 = f"+3937{uuid.uuid4().int % 10**8:08d}"
    esistente = await make_contact(db_session, tenant, e164=e164, display_name="Vecchio Nome")
    riga = await make_discovered_chat(db_session, tenant, number, e164=e164,
                                      display_name="Nome Nuovo Dalla Chat")
    await db_session.commit()

    report = await promozione.promuovi(db_session, tenant_id=tenant.id, ids=[riga.id])

    assert report.contatti_riusati == 1
    assert report.contatti_creati == 0
    assert report.promossi == 1

    n_contatti = await db_session.scalar(
        select(func.count(WaContact.id)).where(WaContact.tenant_id == tenant.id))
    assert n_contatti == 1
    await db_session.refresh(esistente)
    assert esistente.display_name == "Nome Nuovo Dalla Chat"   # gap-fill


@pytest.mark.asyncio
async def test_contatto_opted_out_si_promuove_ma_si_riporta(db_session):
    """L'opt-out non impedisce 'diventare WaContact' (e' gia' un WaContact):
    impedisce solo l'arruolamento in campagna (Task 3), non questo passo."""
    tenant, number = await _ctx(db_session)
    e164 = f"+3938{uuid.uuid4().int % 10**8:08d}"
    esistente = await make_contact(db_session, tenant, e164=e164)
    esistente.opted_out = True
    esistente.do_not_contact = True
    riga = await make_discovered_chat(db_session, tenant, number, e164=e164)
    await db_session.commit()

    report = await promozione.promuovi(db_session, tenant_id=tenant.id, ids=[riga.id])

    assert report.promossi == 1
    assert report.gia_dnc == 1
    assert report.contatti_promossi_ids == []   # non proposto per l'arruolamento

    riga_ricaricata = await db_session.get(type(riga), riga.id)
    assert riga_ricaricata.status == "promosso"


@pytest.mark.asyncio
async def test_gruppo_tra_gli_id_si_scarta_gli_altri_procedono(db_session):
    """Mai un gruppo, per costruzione -- anche iniettato a mano nel batch,
    accanto a righe legittime che devono comunque procedere."""
    tenant, number = await _ctx(db_session)
    riga_gruppo = await make_discovered_chat(db_session, tenant, number,
                                             tipo_chat="gruppo", e164=None,
                                             chat_title="Famiglia Rossi")
    riga_ok = await make_discovered_chat(db_session, tenant, number)
    await db_session.commit()

    report = await promozione.promuovi(db_session, tenant_id=tenant.id,
                                       ids=[riga_gruppo.id, riga_ok.id])

    assert report.promossi == 1
    assert report.scarti == [Scarto(id=riga_gruppo.id, motivo="gruppo")]

    riga_gruppo_ricaricata = await db_session.get(type(riga_gruppo), riga_gruppo.id)
    assert riga_gruppo_ricaricata.status == "nuovo"   # mai promossa


@pytest.mark.asyncio
async def test_doppia_promozione_e_idempotente(db_session):
    """status non torna mai indietro: una seconda promozione sulla stessa
    riga si scarta con 'gia_promosso' e non ricrea un secondo WaContact."""
    tenant, number = await _ctx(db_session)
    riga = await make_discovered_chat(db_session, tenant, number)
    await db_session.commit()

    primo = await promozione.promuovi(db_session, tenant_id=tenant.id, ids=[riga.id])
    assert primo.promossi == 1

    secondo = await promozione.promuovi(db_session, tenant_id=tenant.id, ids=[riga.id])
    assert secondo.promossi == 0
    assert secondo.scarti == [Scarto(id=riga.id, motivo="gia_promosso")]

    n_contatti = await db_session.scalar(
        select(func.count(WaContact.id)).where(WaContact.tenant_id == tenant.id,
                                                WaContact.phone_hmac == riga.phone_hmac))
    assert n_contatti == 1   # non un secondo WaContact

    riga_ricaricata = await db_session.get(type(riga), riga.id)
    assert riga_ricaricata.status == "promosso"   # resta 'promosso', non torna indietro


@pytest.mark.asyncio
async def test_id_sconosciuto_si_scarta(db_session):
    tenant, _number = await _ctx(db_session)
    id_sconosciuto = str(uuid.uuid4())

    report = await promozione.promuovi(db_session, tenant_id=tenant.id, ids=[id_sconosciuto])

    assert report.scarti == [Scarto(id=id_sconosciuto, motivo="non_trovato")]
    assert report.promossi == 0


@pytest.mark.asyncio
async def test_id_con_null_byte_si_scarta_senza_500(db_session):
    """QA di fine modulo (adversarial): un id con '\\x00' faceva risalire un
    CharacterNotInRepertoireError non catturato da asyncpg -- 500 grezzo
    invece di uno scarto gestito. L'id atteso e' sempre un uuid4: si valida
    PRIMA della query, non si lascia decidere al driver."""
    tenant, _number = await _ctx(db_session)
    id_ostile = "abc\x00def"

    report = await promozione.promuovi(db_session, tenant_id=tenant.id, ids=[id_ostile])

    assert report.scarti == [Scarto(id=id_ostile, motivo="non_trovato")]
    assert report.promossi == 0


@pytest.mark.asyncio
async def test_riga_di_altro_tenant_si_scarta(db_session):
    """Il gap IDOR segnalato in review: una riga ESISTE, ha un numero valido
    ed e' promuovibile -- ma appartiene a un tenant diverso da quello con cui
    si chiama `promuovi()`. Deve scartarsi con lo stesso 'non_trovato' di un
    id inesistente (non un motivo diverso, altrimenti si distinguerebbe
    dall'esterno "non esiste" da "e' di un altro tenant" -- fuga di
    informazione). La riga non deve muoversi: resta 'nuovo', non 'promosso',
    e non genera nessun WaContact sotto il tenant chiamante."""
    tenant_proprietario, number_proprietario = await _ctx(db_session)
    tenant_chiamante, _number_chiamante = await _ctx(db_session)
    riga = await make_discovered_chat(db_session, tenant_proprietario, number_proprietario)
    await db_session.commit()

    report = await promozione.promuovi(db_session, tenant_id=tenant_chiamante.id, ids=[riga.id])

    assert report.scarti == [Scarto(id=riga.id, motivo="non_trovato")]
    assert report.promossi == 0
    assert report.contatti_creati == 0

    riga_ricaricata = await db_session.get(type(riga), riga.id)
    assert riga_ricaricata.status == "nuovo"   # mai toccata

    n_contatti_chiamante = await db_session.scalar(
        select(func.count(WaContact.id)).where(WaContact.tenant_id == tenant_chiamante.id))
    assert n_contatti_chiamante == 0   # nessun contatto creato per il tenant sbagliato


@pytest.mark.asyncio
async def test_savepoint_su_corsa_concorrente(db_session):
    """Stesso schema di
    test_wa_ingest.test_ingest_concorrente_stesso_numero_due_campagne_non_va_in_500:
    due righe scoperte DIVERSE (stesso tenant, stesso numero telefono -- lo
    stesso contatto scoperto da due wa_numbers dello stesso negozio), promosse
    in parallelo in due sessioni proprie. La SAVEPOINT deve far convergere
    entrambe senza IntegrityError non catturato, con UN solo WaContact."""
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import settings
    from app.utils.db_dialect import to_async_database_url

    tenant, number_a = await _ctx(db_session)
    number_b = await make_number(db_session, tenant)
    stesso_e164 = f"+3939{uuid.uuid4().int % 10**8:08d}"
    riga_a = await make_discovered_chat(db_session, tenant, number_a, e164=stesso_e164)
    riga_b = await make_discovered_chat(db_session, tenant, number_b, e164=stesso_e164)
    await db_session.commit()

    eng = create_async_engine(to_async_database_url(settings.database_url))
    Session = async_sessionmaker(eng, expire_on_commit=False)

    async def promuovi_in_propria_sessione(riga_id):
        async with Session() as db:
            return await promozione.promuovi(db, tenant_id=tenant.id, ids=[riga_id])

    r_a, r_b = await asyncio.gather(
        promuovi_in_propria_sessione(riga_a.id),
        promuovi_in_propria_sessione(riga_b.id),
    )

    assert [r_a.promossi, r_b.promossi] == [1, 1]      # nessun 500, nessuna riga persa
    assert r_a.contatti_creati + r_b.contatti_creati == 1     # un vincitore la crea
    assert r_a.contatti_riusati + r_b.contatti_riusati == 1   # l'altro la riusa

    async with Session() as check_db:
        n_contatti = await check_db.scalar(
            select(func.count(WaContact.id)).where(WaContact.tenant_id == tenant.id,
                                                    WaContact.phone_hmac == riga_a.phone_hmac))
        assert n_contatti == 1   # dedup regge, un solo WaContact
    await eng.dispose()


@pytest.mark.asyncio
async def test_chat_title_mascherato_non_si_riversa_su_wacontact(db_session):
    """P12: un'etichetta mascherata (numero non in rubrica) non deve mai
    diventare il chat_title NE' il display_name di un WaContact nuovo --
    restano None, come farebbe e_etichetta_mascherata a dirlo. `chat_title` e
    `display_name` di WaDiscoveredChat arrivano SEMPRE dalla stessa sorgente
    (salvataggio.py, etichetta_visibile): il guard deve valere su entrambi,
    non solo su chat_title -- trovato in review, la bozza iniziale copiava
    display_name senza guard."""
    tenant, number = await _ctx(db_session)
    e164 = f"+3931{uuid.uuid4().int % 10**8:08d}"
    riga = await make_discovered_chat(db_session, tenant, number, e164=e164,
                                      chat_title=f"+39{'•' * 5}077", display_name=f"+39{'•' * 5}077")
    await db_session.commit()

    await promozione.promuovi(db_session, tenant_id=tenant.id, ids=[riga.id])

    contatto = await db_session.scalar(
        select(WaContact).where(WaContact.tenant_id == tenant.id,
                                WaContact.phone_hmac == riga.phone_hmac))
    assert contatto.chat_title is None
    assert contatto.display_name is None


@pytest.mark.asyncio
async def test_chat_title_senza_titolo_resta_none(db_session):
    tenant, number = await _ctx(db_session)
    riga = await make_discovered_chat(db_session, tenant, number, chat_title=None,
                                      display_name=None)
    await db_session.commit()

    await promozione.promuovi(db_session, tenant_id=tenant.id, ids=[riga.id])

    contatto = await db_session.scalar(
        select(WaContact).where(WaContact.tenant_id == tenant.id,
                                WaContact.phone_hmac == riga.phone_hmac))
    assert contatto.chat_title is None


@pytest.mark.asyncio
async def test_display_name_mascherato_non_sovrascrive_nome_vero_gia_salvato(db_session):
    """Trovato in review con un test dedicato (era davvero riproducibile,
    non solo un'ipotesi da lettura di codice): un WaContact gia' salvato con
    un nome vero ("Mario Rossi", da un CSV ingest precedente) veniva
    sovrascritto con la maschera "+39•••••077" quando la stessa persona
    veniva ri-scoperta con il pannello info non apribile -- l'opposto del
    principio 'gap-fill che integra, non cancella'."""
    tenant, number = await _ctx(db_session)
    e164 = f"+3936{uuid.uuid4().int % 10**8:08d}"
    esistente = await make_contact(db_session, tenant, e164=e164, display_name="Mario Rossi")
    riga = await make_discovered_chat(db_session, tenant, number, e164=e164,
                                      chat_title=f"+39{'•' * 5}077", display_name=f"+39{'•' * 5}077")
    await db_session.commit()

    report = await promozione.promuovi(db_session, tenant_id=tenant.id, ids=[riga.id])

    assert report.contatti_riusati == 1
    await db_session.refresh(esistente)
    assert esistente.display_name == "Mario Rossi"   # non sovrascritto dalla maschera
