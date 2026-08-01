"""Worker di invio del canale WhatsApp: mini-sessioni per-numero.

Calco dichiarato: services/browser_bio.py (claim atomico, Retry(defer) a
fine sessione, escalation su fallimenti consecutivi), applicato a
wa_campaign_contacts invece che a Follower. Le differenze rispetto a quel
file sono tutte commentate: dove non c'e' commento, e' lo stesso pattern.
"""
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import or_, select, update

from app.config import settings


async def claim_next_wa_contact(db, *, number_id: str, worker_id: str):
    """Prende UNA riga pronta per questo numero e la marca sotto lock.
    Ritorna (cc, contact, campaign, step) oppure None.

    La SELECT e' la query di eleggibilita' del contratto §7.3: se cambia
    qui, cambia il contratto -- non e' un dettaglio di implementazione, e'
    l'interfaccia su cui M2 costruisce le proprie righe.
    """
    from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                               WaContact, WaContactStatus, WaNumber, WaNumberStatus,
                               WaSequenceStep)

    now = datetime.utcnow()
    stale_cutoff = now - timedelta(minutes=int(settings.wa_lock_timeout_min))

    riga = (
        select(WaCampaignContact, WaContact, WaCampaign)
        .join(WaCampaign, WaCampaign.id == WaCampaignContact.campaign_id)
        .join(WaContact, WaContact.id == WaCampaignContact.contact_id)
        .join(WaNumber, WaNumber.id == WaCampaign.wa_number_id)
        .where(
            WaCampaign.status == WaCampaignStatus.running,
            WaNumber.status == WaNumberStatus.active,
            WaNumber.id == number_id,
            WaCampaignContact.status.in_([WaContactStatus.queued,
                                          WaContactStatus.in_sequence]),
            WaCampaignContact.next_action_at.is_not(None),
            WaCampaignContact.next_action_at <= now,
            or_(WaCampaignContact.locked_by.is_(None),
                WaCampaignContact.locked_at < stale_cutoff),
            WaCampaignContact.failure_count < int(settings.wa_max_failures_per_contact),
            WaContact.opted_out.is_(False),
            WaContact.do_not_contact.is_(False),
        )
        .order_by(WaCampaignContact.next_action_at)
        .limit(1)
    )
    result = (await db.execute(riga)).first()
    if result is None:
        return None
    cc, contact, campaign = result

    # Claim atomico: la WHERE ripete la condizione di lock. Se un altro
    # worker ha vinto la corsa fra SELECT e UPDATE, rowcount e' 0 e qui si
    # esce senza errore -- stesso pattern di browser_bio.claim_next_pending.
    claim = await db.execute(
        update(WaCampaignContact)
        .where(
            WaCampaignContact.id == cc.id,
            or_(WaCampaignContact.locked_by.is_(None),
                WaCampaignContact.locked_at < stale_cutoff),
        )
        .values(locked_by=worker_id, locked_at=now)
    )
    await db.commit()
    if (claim.rowcount or 0) == 0:
        logger.debug(f"claim perso su {cc.id} (un altro worker e' arrivato prima)")
        return None

    step = await db.scalar(
        select(WaSequenceStep).where(
            WaSequenceStep.campaign_id == campaign.id,
            WaSequenceStep.step_index == (cc.current_step or -1) + 1,
        )
    )
    if step is None:
        # Contatto senza step successivo: non e' lavoro, e' una riga da
        # chiudere. Si rilascia il lock e si lascia al chiamante decidere.
        await db.execute(update(WaCampaignContact).where(WaCampaignContact.id == cc.id)
                         .values(locked_by=None, locked_at=None,
                                 status=WaContactStatus.completed, next_action_at=None))
        await db.commit()
        return None

    await db.refresh(cc)
    return cc, contact, campaign, step
