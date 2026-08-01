"""QA M3 (Task 15 Step 3), gruppo J (36-39): invarianti SQL a fine batch.

Nome del file scelto apposta per essere l'ULTIMO raccolto da pytest fra i
file `test_wa_*`/`test_zz_*` (ordine alfabetico di default): questi test
leggono lo stato PERSISTITO (una sessione fresca, non db_session che fa
rollback a fine test) dopo che tutto il resto della suite WA ha gia' girato
e scritto/commesso le sue righe -- e' esattamente il "fine dell'intero
batch di test di questo file" richiesto da qa-m3-adversarial.md gruppo J.
"""
import pytest
from sqlalchemy import func, select

from app.database import AsyncSessionLocal


@pytest.mark.asyncio
async def test_36_nessun_lock_piu_vecchio_del_timeout():
    from datetime import datetime, timedelta
    from app.config import settings
    from app.models.wa import WaCampaignContact, WaContactStatus

    cutoff = datetime.utcnow() - timedelta(minutes=int(settings.wa_lock_timeout_min))
    async with AsyncSessionLocal() as db:
        righe = (await db.execute(
            select(WaCampaignContact.id).where(
                WaCampaignContact.status.in_([WaContactStatus.queued,
                                              WaContactStatus.in_sequence]),
                WaCampaignContact.locked_by.is_not(None),
                WaCampaignContact.locked_at < cutoff,
            )
        )).scalars().all()
    assert righe == [], f"lock piu' vecchi del timeout: {righe}"


@pytest.mark.asyncio
async def test_37_nessun_messaggio_fermo_in_sending():
    from app.models.wa import WaMessage, WaMessageStatus

    async with AsyncSessionLocal() as db:
        righe = (await db.execute(
            select(WaMessage.id).where(WaMessage.status == WaMessageStatus.sending)
        )).scalars().all()
    assert righe == [], f"messaggi fermi in sending: {righe}"


@pytest.mark.asyncio
async def test_38_contatore_sent_coerente_col_conteggio_reale():
    from app.models.wa import WaCampaign, WaMessage, WaMessageStatus

    async with AsyncSessionLocal() as db:
        campagne = (await db.execute(select(WaCampaign.id, WaCampaign.sent))).all()
        for campaign_id, sent in campagne:
            reale = await db.scalar(
                select(func.count(WaMessage.id)).where(
                    WaMessage.campaign_id == campaign_id,
                    WaMessage.status == WaMessageStatus.sent))
            assert sent == reale, (
                f"campagna {campaign_id}: sent={sent} ma wa_messages reali={reale}")


@pytest.mark.asyncio
async def test_39_nessun_contatto_optato_con_righe_non_terminali():
    from app.models.wa import WaCampaignContact, WaContact, WaContactStatus

    terminali = (WaContactStatus.opted_out, WaContactStatus.completed,
                WaContactStatus.skipped, WaContactStatus.replied)
    async with AsyncSessionLocal() as db:
        optati = (await db.execute(
            select(WaContact.id).where(WaContact.opted_out.is_(True))
        )).scalars().all()
        for contact_id in optati:
            residue = (await db.execute(
                select(WaCampaignContact.id).where(
                    WaCampaignContact.contact_id == contact_id,
                    WaCampaignContact.status.notin_(terminali))
            )).scalars().all()
            assert residue == [], (
                f"contatto opted_out {contact_id} con righe non terminali: {residue}")
