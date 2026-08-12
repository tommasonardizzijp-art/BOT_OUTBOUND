"""Test dei KPI di campagna (Task 8): GET /api/wa/campaigns/{id}/kpi.

M2 legge soltanto i contatori denormalizzati (sent/replied/opted_out/failed
li scrive M3/M4, contratto 4.1): qui si prova che la lettura non divida mai
per zero e che i tassi si calcolino sugli INVIATI, non sui caricati.
"""
import pytest

from app.api import wa_campaigns
from tests.factories_wa import make_campaign, make_number, make_tenant


@pytest.mark.asyncio
async def test_kpi_su_campagna_vuota_non_divide_per_zero(db_session):
    """Il caso limite piu' banale e' quello che rompe davvero le dashboard."""
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    await db_session.commit()
    kpi = await wa_campaigns.kpi(campaign.id, db=db_session)
    assert kpi["tasso_risposta"] == 0
    assert kpi["tasso_optout"] == 0


@pytest.mark.asyncio
async def test_kpi_derivati_calcolati_sugli_inviati_non_sui_caricati(db_session):
    """Il tasso di risposta si misura su chi ha ricevuto, non su chi e' in
    lista: altrimenti una campagna appena partita sembra un disastro."""
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    campaign.total_contacts, campaign.sent, campaign.replied = 100, 20, 5
    campaign.opted_out, campaign.failed = 2, 3
    await db_session.commit()
    kpi = await wa_campaigns.kpi(campaign.id, db=db_session)
    assert kpi["tasso_risposta"] == 25.0     # 5/20
    assert kpi["tasso_optout"] == 10.0       # 2/20


@pytest.mark.asyncio
async def test_da_inviare_non_conta_chi_non_ricevera_mai(db_session):
    """'da inviare' era una sottrazione, non un conteggio: total_contacts
    meno gli inviati. Cosi' ci finiva dentro chiunque non avesse ricevuto,
    compreso chi non ricevera' MAI.

    Misurato sulla campagna Primero del 12/08: la dashboard diceva 193 da
    inviare, ma 6 erano `skipped` per 'no_existing_chat' -- contatti senza una
    chat preesistente, che la regola v2 del canale non contatta e che hanno
    next_action_at NULL. Ne sarebbero partiti 187. Il numero non sarebbe mai
    arrivato a zero: a campagna finita la dashboard avrebbe continuato a
    promettere 6 messaggi che non esistono.

    Peggiora da solo nel tempo: ogni skipped, e ogni opt-out arrivato prima
    del nostro invio, resta contato per sempre fra i 'da inviare'.

    Si contano le righe che possono ancora partire -- queued e in_sequence --
    e nient'altro."""
    from app.models.wa import WaContactStatus
    from tests.factories_wa import make_campaign_contact, make_contact

    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    campaign.total_contacts, campaign.sent = 6, 2

    attesi = [WaContactStatus.queued, WaContactStatus.queued,
              WaContactStatus.in_sequence]
    mai = [WaContactStatus.skipped, WaContactStatus.completed,
           WaContactStatus.opted_out]
    for stato in attesi + mai:
        contatto = await make_contact(db_session, tenant)
        await make_campaign_contact(db_session, campaign, contatto, status=stato)
    await db_session.commit()

    kpi = await wa_campaigns.kpi(campaign.id, db=db_session)
    assert kpi["da_inviare"] == 3, (
        "conta anche chi non partira' mai (skipped/completed/opted_out)")


@pytest.mark.asyncio
async def test_kpi_segnala_la_soglia_di_allarme_optout(db_session):
    """SDD 10.3: oltre il 5% di opt-out la campagna va guardata. Il flag e'
    informativo: mettere in pausa e' una decisione di Tommaso."""
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    campaign.sent, campaign.opted_out = 100, 6
    await db_session.commit()
    kpi = await wa_campaigns.kpi(campaign.id, db=db_session)
    assert kpi["allarme_optout"] is True


@pytest.mark.asyncio
async def test_kpi_con_optout_prima_ancora_di_un_invio_non_esplode(db_session):
    """Caso limite non coperto dal piano: un contatto puo' opt-out-are (es.
    da un altro canale, o da un DNC importato) PRIMA che gli sia mai stato
    inviato un messaggio della campagna. sent=0 e opted_out>0 non deve dare
    ZeroDivisionError ne' un tasso infinito/negativo: il conteggio assoluto
    resta visibile, il tasso (che ha senso solo su chi ha ricevuto qualcosa)
    resta a zero invece di mentire con un 'infinito per cento'."""
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    campaign.total_contacts, campaign.sent, campaign.opted_out = 10, 0, 3
    await db_session.commit()
    kpi = await wa_campaigns.kpi(campaign.id, db=db_session)
    assert kpi["inviati"] == 0
    assert kpi["optout"] == 3
    assert kpi["tasso_optout"] == 0.0
    assert kpi["allarme_optout"] is False


@pytest.mark.asyncio
async def test_kpi_con_contatore_sopra_gli_inviati_non_mostra_oltre_100(db_session):
    """Trovato in review: sent/opted_out/failed sono scritti da M3 (non
    ancora costruito), senza vincolo a DB che impedisca n > inviati. Senza
    clamp, sent=1 e opted_out=1000 dava un tasso di 100000%."""
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    campaign.sent, campaign.opted_out = 1, 1000
    await db_session.commit()
    kpi = await wa_campaigns.kpi(campaign.id, db=db_session)
    assert kpi["tasso_optout"] == 100.0


@pytest.mark.asyncio
async def test_kpi_con_contatore_negativo_non_mostra_sotto_zero(db_session):
    """Stesso rischio del test sopra, lato opposto: il clamp originale
    copriva solo il limite superiore (min(100, ...)), non quello inferiore
    -- un contatore negativo dava un tasso tipo -1000%."""
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _ = await make_campaign(db_session, tenant, number)
    campaign.sent, campaign.opted_out = 10, -5
    await db_session.commit()
    kpi = await wa_campaigns.kpi(campaign.id, db=db_session)
    assert kpi["tasso_optout"] == 0.0
