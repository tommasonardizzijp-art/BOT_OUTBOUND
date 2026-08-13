"""§5 AVVIO 12/08: rete di sicurezza a livello DB contro un secondo invio
sullo stesso (campaign_id, contact_id, step_index) -- prima esisteva solo
il controllo applicativo in wa_sender.py. L'indice e' PARZIALE (solo
status IN ('sending', 'sent')), non una UniqueConstraint piena: un retry
dopo 'failed' deve restare possibile."""
import pytest
from sqlalchemy.exc import IntegrityError

from app.models.wa import WaCampaignStatus, WaMessage, WaMessageStatus
from tests.factories_wa import make_campaign, make_contact, make_number, make_tenant


async def _messaggio(campaign, contact, number, *, status, step_index=0):
    return WaMessage(campaign_id=campaign.id, contact_id=contact.id,
                     wa_number_id=number.id, step_index=step_index,
                     template_variant="a", rendered_text="ciao", status=status)


@pytest.mark.asyncio
async def test_due_sent_sullo_stesso_step_violano_indice(db_session):
    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    campagna, _ = await make_campaign(db_session, tenant, numero,
                                      status=WaCampaignStatus.running)

    db_session.add(await _messaggio(campagna, contatto, numero, status=WaMessageStatus.sent))
    await db_session.flush()

    db_session.add(await _messaggio(campagna, contatto, numero, status=WaMessageStatus.sent))
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_retry_dopo_failed_resta_possibile(db_session):
    """Il caso che una UniqueConstraint piena romperebbe: wa_sender.py
    permette esplicitamente un nuovo tentativo quando il precedente e'
    'failed' -- l'indice parziale non deve bloccarlo."""
    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    campagna, _ = await make_campaign(db_session, tenant, numero,
                                      status=WaCampaignStatus.running)

    db_session.add(await _messaggio(campagna, contatto, numero, status=WaMessageStatus.failed))
    await db_session.flush()

    db_session.add(await _messaggio(campagna, contatto, numero, status=WaMessageStatus.sent))
    await db_session.flush()  # non deve sollevare


@pytest.mark.asyncio
async def test_step_diversi_non_collidono(db_session):
    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    campagna, _ = await make_campaign(db_session, tenant, numero,
                                      status=WaCampaignStatus.running)

    db_session.add(await _messaggio(campagna, contatto, numero, status=WaMessageStatus.sent, step_index=0))
    await db_session.flush()
    db_session.add(await _messaggio(campagna, contatto, numero, status=WaMessageStatus.sent, step_index=1))
    await db_session.flush()  # non deve sollevare
