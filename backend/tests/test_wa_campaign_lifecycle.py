"""Ciclo di vita campagna (Task 7): avvia/pausa/riprendi/ferma.

Le due cose che devono funzionare (piano 2026-07-29, Task 7):
1. Le validazioni di start (SDD 8.1): numero active, almeno uno step,
   almeno un contatto, e max 1 campagna 'running' per numero (Q2, 23/07).
2. La ri-stampa di next_action_at allo start e ad ogni resume (contratto
   7.2), senza mai toccare le righe terminali (next_action_at=NULL per
   una ragione precisa).

Confine con M3 (contratto 4.1): running->completed e running->error sono
SOLO di M3. Questo servizio non li scrive mai.
"""
from datetime import datetime, timedelta

import pytest

from app.models.wa import WaCampaignStatus, WaNumberStatus
from app.services import wa_campaign_service as svc
from tests.factories_wa import (make_campaign, make_campaign_contact, make_contact,
                                make_number, make_tenant)


async def _pronta(db):
    tenant = await make_tenant(db)
    number = await make_number(db, tenant)
    campaign, _ = await make_campaign(db, tenant, number)
    contact = await make_contact(db, tenant)
    cc = await make_campaign_contact(db, campaign, contact)
    await db.commit()
    return tenant, number, campaign, cc


@pytest.mark.asyncio
async def test_start_valida_e_porta_a_running(db_session):
    _, _, campaign, _ = await _pronta(db_session)
    await svc.avvia(db_session, campaign.id)
    await db_session.refresh(campaign)
    assert campaign.status == WaCampaignStatus.running
    assert campaign.started_at is not None


@pytest.mark.asyncio
async def test_start_ristampa_next_action_at_sulle_righe_queued(db_session):
    """Contratto 7.2: una campagna ingerita e lasciata in bozza tre
    settimane non deve presentarsi al worker come righe scadute da giorni."""
    _, _, campaign, cc = await _pronta(db_session)
    cc.next_action_at = datetime.utcnow() - timedelta(days=21)
    await db_session.commit()

    await svc.avvia(db_session, campaign.id)
    await db_session.refresh(cc)
    assert cc.next_action_at > datetime.utcnow() - timedelta(minutes=1)


@pytest.mark.asyncio
async def test_start_rifiutato_se_il_numero_non_e_active(db_session):
    _, number, campaign, _ = await _pronta(db_session)
    number.status = WaNumberStatus.qr_required
    await db_session.commit()
    with pytest.raises(ValueError):
        await svc.avvia(db_session, campaign.id)


@pytest.mark.asyncio
async def test_start_rifiutato_senza_contatti(db_session):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    await db_session.commit()
    with pytest.raises(ValueError):
        await svc.avvia(db_session, campaign.id)


@pytest.mark.asyncio
async def test_una_sola_campagna_running_per_numero(db_session):
    """Decisione 23/07 (Q2): due campagne sullo stesso numero
    significherebbero ritmo doppio, e il pacing e' per-job."""
    tenant, number, campaign_a, _ = await _pronta(db_session)
    await svc.avvia(db_session, campaign_a.id)
    campaign_b, _ = await make_campaign(db_session, tenant, number, name="seconda")
    contact_b = await make_contact(db_session, tenant)
    await make_campaign_contact(db_session, campaign_b, contact_b)
    await db_session.commit()
    with pytest.raises(ValueError):
        await svc.avvia(db_session, campaign_b.id)


@pytest.mark.asyncio
async def test_doppio_start_non_e_un_no_op_silenzioso(db_session):
    _, _, campaign, _ = await _pronta(db_session)
    await svc.avvia(db_session, campaign.id)
    with pytest.raises(ValueError):
        await svc.avvia(db_session, campaign.id)


@pytest.mark.asyncio
async def test_resume_rispalma_ma_non_tocca_le_righe_terminali(db_session):
    from app.models.wa import WaContactStatus
    _, _, campaign, cc = await _pronta(db_session)
    await svc.avvia(db_session, campaign.id)
    await svc.pausa(db_session, campaign.id)
    cc.status = WaContactStatus.opted_out
    cc.next_action_at = None
    await db_session.commit()

    await svc.riprendi(db_session, campaign.id)
    await db_session.refresh(cc)
    assert cc.next_action_at is None          # terminale: non si risveglia
    assert cc.status == WaContactStatus.opted_out


@pytest.mark.asyncio
async def test_stop_non_cancella_niente(db_session):
    """'stopped' e' uno stato, non una cancellazione: i KPI restano."""
    from sqlalchemy import func, select
    from app.models.wa import WaCampaignContact
    _, _, campaign, _ = await _pronta(db_session)
    await svc.avvia(db_session, campaign.id)
    await svc.ferma(db_session, campaign.id)
    await db_session.refresh(campaign)
    assert campaign.status == WaCampaignStatus.stopped
    assert await db_session.scalar(select(func.count(WaCampaignContact.id))
                                   .where(WaCampaignContact.campaign_id == campaign.id)) == 1


@pytest.mark.asyncio
async def test_due_avvia_concorrenti_sullo_stesso_numero_non_passano_entrambe(db_session):
    """Trovato in review dedicata: la sola SELECT-poi-UPDATE lasciava una
    finestra TOCTOU reale -- due avvia() in due sessioni DB indipendenti,
    lanciate insieme, passavano ENTRAMBE (riprodotto con asyncio.gather,
    non sequenziale). L'UPDATE atomico con NOT EXISTS dentro la stessa
    istruzione chiude la finestra: qui si verifica sotto concorrenza vera,
    non solo con chiamate in sequenza come gli altri test di questo file."""
    import asyncio

    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import settings
    from app.models.wa import WaCampaign, WaCampaignStatus
    from app.utils.db_dialect import to_async_database_url

    tenant, number, campaign_a, _ = await _pronta(db_session)
    campaign_b, _ = await make_campaign(db_session, tenant, number, name="B")
    contact_b = await make_contact(db_session, tenant)
    await make_campaign_contact(db_session, campaign_b, contact_b)
    await db_session.commit()

    eng = create_async_engine(to_async_database_url(settings.database_url))
    Session = async_sessionmaker(eng, expire_on_commit=False)

    async def avvia_in_propria_sessione(campaign_id):
        async with Session() as db:
            try:
                await svc.avvia(db, campaign_id)
                return True
            except ValueError:
                return False

    esiti = await asyncio.gather(
        avvia_in_propria_sessione(campaign_a.id),
        avvia_in_propria_sessione(campaign_b.id),
    )
    assert sorted(esiti) == [False, True]     # una passa, una viene rifiutata: mai entrambe

    n_running = await db_session.scalar(
        select(func.count(WaCampaign.id)).where(WaCampaign.wa_number_id == number.id,
                                                 WaCampaign.status == WaCampaignStatus.running))
    assert n_running == 1
    await eng.dispose()
