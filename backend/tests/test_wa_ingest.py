"""Test di backend/app/services/wa_ingest.py (Task 3 del piano M2).

Le tre regole del flusso (SDD 7.1), ognuna con un test dedicato: numero
invalido -> scarto con motivo (mai aggiustato in silenzio); contatto
opted_out/do_not_contact -> escluso e riportato (l'opt-out vince SEMPRE
sull'ingest); duplicato nel file -> dedup.

Nota deviazione dal piano: test_nessun_numero_in_chiaro_nei_log usa un sink
loguru dedicato invece di `caplog`. Il repo non ha da nessuna parte una
configurazione che faccia propagare loguru verso il logging stdlib (grep su
tutta la codebase: nessun `PropagateHandler`/`logger.add` verso `logging`);
`caplog` cattura solo i record dello stdlib `logging`, quindi con loguru
`caplog.text` resta sempre vuoto e il test passerebbe anche con un vero leak
nel messaggio. Il pattern corretto e' gia' in uso in test_wa_session.py:
`loguru_logger.add(lambda m: messages.append(str(m)), level=...)`.
"""
import pytest
from loguru import logger as loguru_logger

from app.services import wa_ingest
from tests.factories_wa import make_campaign, make_contact, make_number, make_tenant


async def _ctx(db):
    tenant = await make_tenant(db)
    number = await make_number(db, tenant)
    campaign, _ = await make_campaign(db, tenant, number)
    await db.commit()
    return tenant, campaign


@pytest.mark.asyncio
async def test_ingest_crea_contatti_e_righe_campagna(db_session):
    tenant, campaign = await _ctx(db_session)
    csv = b"numero,nome,ultimo_ordine\n+393331112223,Marco,10/01/2026\n3334445556,Anna,\n"
    report = await wa_ingest.ingerisci_csv(
        db_session, tenant_id=tenant.id, campaign_id=campaign.id, contenuto=csv)
    assert report.creati == 2
    assert report.scarti == []

    from sqlalchemy import func, select
    from app.models.wa import WaCampaignContact, WaContact, WaContactStatus
    assert await db_session.scalar(
        select(func.count(WaContact.id)).where(WaContact.tenant_id == tenant.id)) == 2
    righe = (await db_session.execute(
        select(WaCampaignContact).where(WaCampaignContact.campaign_id == campaign.id)
    )).scalars().all()
    assert len(righe) == 2
    # Contratto di consegna §7.1 + invarianti I1/I3
    for cc in righe:
        assert cc.status == WaContactStatus.queued
        assert cc.current_step == -1
        assert cc.next_action_at is not None
        assert cc.locked_by is None and cc.locked_at is None
        assert cc.failure_count == 0


@pytest.mark.asyncio
async def test_numero_senza_prefisso_prende_il_default_paese(db_session):
    tenant, campaign = await _ctx(db_session)
    report = await wa_ingest.ingerisci_csv(
        db_session, tenant_id=tenant.id, campaign_id=campaign.id,
        contenuto=b"numero\n3334445556\n")
    assert report.creati == 1
    from sqlalchemy import select
    from app.models.wa import WaContact
    from app.utils.crypto import decrypt
    c = await db_session.scalar(select(WaContact).where(WaContact.tenant_id == tenant.id))
    assert decrypt(c.encrypted_phone) == "+393334445556"


@pytest.mark.asyncio
async def test_numeri_plausibilmente_sbagliati_vengono_scartati_non_aggiustati(db_session):
    """I casi negativi utili sono quelli PLAUSIBILI: '' e 'abc' non hanno mai
    intercettato niente, '+39 342 146 0077 ext. 12' si' -- ed era un numero
    che diventava un numero DIVERSO, accettato in silenzio."""
    tenant, campaign = await _ctx(db_session)
    csv = ("numero\n"
           "+39 342 146 0077 ext. 12\n"
           "+39 342 146 0077 (casa)\n"
           "0039 342 146 0078\n"
           "+39-342-146-0079\n"
           "342.146.0080\n"
           "+391\n"
           "+3934214600771234567890\n").encode()
    report = await wa_ingest.ingerisci_csv(
        db_session, tenant_id=tenant.id, campaign_id=campaign.id, contenuto=csv)
    assert report.creati + len(report.scarti) == 7
    # Nessun numero "aggiustato": tutto cio' che non e' normalizzabile in
    # modo NON ambiguo deve essere uno scarto con motivo.
    assert len(report.scarti) >= 3
    for s in report.scarti:
        assert s.motivo
        assert "0077" not in s.valore_mascherato or s.valore_mascherato.count("•") > 0


@pytest.mark.asyncio
async def test_duplicati_nel_file_contati_una_volta_sola(db_session):
    tenant, campaign = await _ctx(db_session)
    csv = b"numero,nome\n+393331112223,Marco\n+39 333 111 2223,Marco B\n"
    report = await wa_ingest.ingerisci_csv(
        db_session, tenant_id=tenant.id, campaign_id=campaign.id, contenuto=csv)
    assert report.creati == 1
    assert report.duplicati_nel_file == 1


@pytest.mark.asyncio
async def test_doppio_upload_dello_stesso_file_non_duplica_nulla(db_session):
    """Q21: l'ingest e' idempotente, il re-upload sana un import interrotto."""
    tenant, campaign = await _ctx(db_session)
    csv = b"numero,nome\n+393331112223,Marco\n"
    primo = await wa_ingest.ingerisci_csv(
        db_session, tenant_id=tenant.id, campaign_id=campaign.id, contenuto=csv)
    secondo = await wa_ingest.ingerisci_csv(
        db_session, tenant_id=tenant.id, campaign_id=campaign.id, contenuto=csv)
    assert primo.creati == 1 and secondo.creati == 0
    from sqlalchemy import func, select
    from app.models.wa import WaCampaignContact
    assert await db_session.scalar(select(func.count(WaCampaignContact.id))
                                   .where(WaCampaignContact.campaign_id == campaign.id)) == 1


@pytest.mark.asyncio
async def test_re_upload_aggiorna_gli_attributi_ma_non_l_optout(db_session):
    """Q16 + SDD 7.5.5: gli attributi si aggiornano, l'opt-out vince
    sull'ingest e non si riattiva MAI da un file."""
    tenant, campaign = await _ctx(db_session)
    await wa_ingest.ingerisci_csv(
        db_session, tenant_id=tenant.id, campaign_id=campaign.id,
        contenuto=b"numero,citta\n+393331112223,Roma\n")
    from sqlalchemy import select
    from app.models.wa import WaContact, WaDncReason
    c = await db_session.scalar(select(WaContact).where(WaContact.tenant_id == tenant.id))
    c.opted_out = True
    c.do_not_contact = True
    c.dnc_reason = WaDncReason.optout
    await db_session.commit()

    report = await wa_ingest.ingerisci_csv(
        db_session, tenant_id=tenant.id, campaign_id=campaign.id,
        contenuto=b"numero,citta\n+393331112223,Milano\n")
    await db_session.refresh(c)
    assert c.opted_out is True          # non riattivato
    assert report.gia_dnc == 1
    assert c.attributes.get("citta") == "Milano"   # attributi aggiornati


@pytest.mark.asyncio
async def test_contatto_dnc_non_entra_in_campagna(db_session):
    tenant, campaign = await _ctx(db_session)
    contact = await make_contact(db_session, tenant, e164="+393331112223")
    contact.do_not_contact = True
    await db_session.commit()

    report = await wa_ingest.ingerisci_csv(
        db_session, tenant_id=tenant.id, campaign_id=campaign.id,
        contenuto=b"numero\n+393331112223\n")
    assert report.gia_dnc == 1
    from sqlalchemy import func, select
    from app.models.wa import WaCampaignContact
    assert await db_session.scalar(select(func.count(WaCampaignContact.id))
                                   .where(WaCampaignContact.campaign_id == campaign.id)) == 0


@pytest.mark.asyncio
async def test_attributi_oltre_il_limite_vengono_troncati_non_salvati_interi(db_session, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "wa_ingest_max_attrs_bytes", 64)
    tenant, campaign = await _ctx(db_session)
    lungo = "x" * 5000
    report = await wa_ingest.ingerisci_csv(
        db_session, tenant_id=tenant.id, campaign_id=campaign.id,
        contenuto=f"numero,note\n+393331112223,{lungo}\n".encode())
    assert report.creati == 1
    from sqlalchemy import select
    from app.models.wa import WaContact
    c = await db_session.scalar(select(WaContact).where(WaContact.tenant_id == tenant.id))
    import json
    assert len(json.dumps(c.attributes)) <= 200


@pytest.mark.asyncio
async def test_nessun_numero_in_chiaro_nei_log(db_session):
    """Contratto §2.3: il primo chiamante di PhoneNormalizationError e'
    questo. Un logger.error(str(exc)) scriverebbe il numero nei log.

    Sink loguru dedicato invece di caplog: caplog cattura solo lo stdlib
    `logging`, e loguru non ci propaga da nessuna parte in questo repo (vedi
    docstring del modulo)."""
    tenant, campaign = await _ctx(db_session)
    messages: list[str] = []
    sink_id = loguru_logger.add(lambda m: messages.append(str(m)), level="DEBUG")
    try:
        await wa_ingest.ingerisci_csv(
            db_session, tenant_id=tenant.id, campaign_id=campaign.id,
            contenuto=b"numero\n+39 342 146 0077 ext. 12\n+393421460078\n")
    finally:
        loguru_logger.remove(sink_id)
    testo = "\n".join(messages)
    assert "3421460077" not in testo
    assert "3421460078" not in testo
    assert "+39342" not in testo


@pytest.mark.asyncio
async def test_lo_scoping_per_tenant_non_si_rompe(db_session):
    """Q20: due tenant con lo stesso numero contatto = due wa_contacts
    distinti, e nessuna query deve incrociarli."""
    tenant_a, campaign_a = await _ctx(db_session)
    tenant_b = await make_tenant(db_session, name="Altro")
    number_b = await make_number(db_session, tenant_b)
    campaign_b, _ = await make_campaign(db_session, tenant_b, number_b)
    await db_session.commit()

    csv = b"numero\n+393331112223\n"
    a = await wa_ingest.ingerisci_csv(db_session, tenant_id=tenant_a.id,
                                      campaign_id=campaign_a.id, contenuto=csv)
    b = await wa_ingest.ingerisci_csv(db_session, tenant_id=tenant_b.id,
                                      campaign_id=campaign_b.id, contenuto=csv)
    assert a.creati == 1 and b.creati == 1
