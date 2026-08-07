"""Unica verita' su quali follower sono lavorabili per una campagna.

Perche' esiste: la lista degli stati era scritta a mano in tre punti di
campaign_orchestrator con tre significati diversi, ed era gia' divergente (il
terzo includeva pending_approval, gli altri no). Con l'arrivo del livello
'none' — dove i follower restano in 'pending' e sono comunque mandabili — tre
liste da tenere allineate sarebbero diventate tre modi di perdere lead in
silenzio. Stesso criterio di SCRAPING_ACTIVE_STATES in app/models/campaign.py.

Due domande DIVERSE, due funzioni:
  - "posso mandare un DM a questo follower?"  -> sendable_*
  - "resta lavoro da fare su questa campagna?" -> remaining_work_*
La seconda include la coda di approvazione: sono follower su cui il lavoro non
e' finito, anche se in questo istante non sono mandabili.
"""
from sqlalchemy import or_

from app.models.campaign import ENRICHMENT_NONE
from app.models.follower import Follower, FollowerStatus

# Stati mandabili quando la campagna arricchisce (bio o contacts).
_SENDABLE_BASE = (FollowerStatus.bio_scraped, FollowerStatus.message_generated)


def _senza_arricchimento(campaign) -> bool:
    """True se la campagna non prevede una visita dedicata al profilo.
    Difensivo: un oggetto senza il campo si comporta come prima dell'introduzione
    del livello (porta chiusa), mai come 'none'."""
    return getattr(campaign, "enrichment_level", None) == ENRICHMENT_NONE


def sendable_statuses(campaign) -> tuple[FollowerStatus, ...]:
    """Stati in cui un follower puo' ricevere un DM."""
    if _senza_arricchimento(campaign):
        # Senza Fase Bio il follower resta come lo lascia la Fase Lista.
        return (FollowerStatus.pending,) + _SENDABLE_BASE
    return _SENDABLE_BASE


def is_sendable(campaign, status: FollowerStatus) -> bool:
    """Predicato su un singolo follower (usato nel ricontrollo pre-invio)."""
    return status in sendable_statuses(campaign)


def sendable_filter(campaign):
    """Condizione SQLAlchemy: i follower a cui si puo' mandare un DM."""
    return Follower.status.in_(list(sendable_statuses(campaign)))


def remaining_work_statuses(campaign) -> tuple[FollowerStatus, ...]:
    """Stati che contano come lavoro ancora da fare sulla campagna.
    Superset dei mandabili: include la coda di approvazione."""
    return sendable_statuses(campaign) + (FollowerStatus.pending_approval,)


def remaining_work_filter(campaign):
    """Condizione SQLAlchemy per "resta lavoro?": stati non terminali OPPURE
    follower attualmente lockato da un worker."""
    return or_(
        Follower.status.in_(list(remaining_work_statuses(campaign))),
        Follower.locked_by_account_id.isnot(None),
    )
