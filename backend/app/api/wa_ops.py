"""Operativita' del canale WhatsApp: kill-switch, stato, avvio manuale.

M3 e' backend puro (decisione 29/07): non c'e' UI in questo modulo. Le
pagine, quando arriveranno, le costruisce M2 contro questi endpoint.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select

from app.database import get_db
from app.models.wa import (WaCampaign, WaCampaignStatus, WaContact, WaDncReason,
                           WaMessage, WaMessageStatus, WaNumber, WaNumberStatus)
from app.services import bot_state_service
from app.workers.wa_worker import enqueue_wa_workers

router = APIRouter(prefix="/wa/ops", tags=["wa-ops"])


class HaltRequest(BaseModel):
    reason: str


class RevokeOptoutRequest(BaseModel):
    note: str

    @field_validator("note")
    @classmethod
    def _note_non_vuota(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("note e' obbligatoria: la revoca di un opt-out va sempre "
                             "giustificata (GDPR/ePrivacy)")
        return v


@router.get("/status")
async def wa_ops_status(db=Depends(get_db)) -> dict:
    from app.config import settings
    oggi = func.date(WaMessage.sent_at) == func.date(func.now())
    return {
        "wa_halted": await bot_state_service.is_wa_halted(db),
        "send_enabled": bool(settings.wa_send_enabled),
        "numeri_attivi": await db.scalar(
            select(func.count(WaNumber.id)).where(WaNumber.status == WaNumberStatus.active)),
        "campagne_running": await db.scalar(
            select(func.count(WaCampaign.id)).where(WaCampaign.status == WaCampaignStatus.running)),
        "inviati_oggi": await db.scalar(
            select(func.count(WaMessage.id)).where(
                WaMessage.status == WaMessageStatus.sent, oggi)) or 0,
    }


@router.post("/halt")
async def wa_ops_halt(body: HaltRequest, db=Depends(get_db)) -> dict:
    await bot_state_service.halt_wa(reason=body.reason, by="api", db=db)
    await db.commit()
    return {"wa_halted": True, "reason": body.reason}


@router.post("/resume")
async def wa_ops_resume(db=Depends(get_db)) -> dict:
    await bot_state_service.resume_wa(by="api", db=db)
    await db.commit()
    return {"wa_halted": False}


@router.post("/campaigns/{campaign_id}/kick")
async def kick_campaign(campaign_id: str, db=Depends(get_db)) -> dict:
    """Riaccoda il worker di invio per la campagna. Serve dopo un resume o
    quando un job e' andato perso. Non forza nulla: se la campagna non e'
    running, non accoda -- lo start delle campagne e' di M2."""
    campaign = await db.scalar(select(WaCampaign).where(WaCampaign.id == campaign_id))
    if campaign is None:
        raise HTTPException(status_code=404, detail="campagna inesistente")
    if campaign.status != WaCampaignStatus.running:
        return {"accodati": 0, "motivo": f"campagna in stato {campaign.status.value}"}
    return {"accodati": await enqueue_wa_workers(campaign_id)}


@router.post("/campaigns/{campaign_id}/resume")
async def resume_campaign(campaign_id: str, db=Depends(get_db)) -> dict:
    """Rimette a 'running' una campagna 'paused' e riaccoda il suo worker.

    ECCEZIONE DICHIARATA E TEMPORANEA al contratto M2/M3 (docs/whatsapp/
    contratto-M2-M3.md, §4.1): la transizione wa_campaigns.status
    paused->running e' di proprieta' SCRITTURA di M2 ("draft->running,
    running<->paused, ->stopped"). Questo endpoint la scrive comunque
    perche' il cron wa_session_healthcheck (cron_worker.py) puo' mettere
    una campagna in paused quando la sessione WhatsApp del numero cade, e
    oggi non esiste ALCUN modo di farla ripartire: kick_campaign rifiuta
    esplicitamente tutto cio' che non e' gia' running, e M2 non ha ancora
    una UI che scriva questa transizione (modulo non ancora costruito).

    Autorizzato a voce dal titolare del progetto, fuori da questo repo,
    come sblocco pragmatico -- NON un emendamento formale del contratto
    (che resta §4.1 cosi' com'e' scritto) e NON un sostituto della UI che
    M2 costruira': quando quella UI esiste, questo endpoint torna
    ridondante e andrebbe ritirato o ridotto a solo-kick.

    Nota di scope: non ri-stampa next_action_at su wa_campaign_contacts
    come farebbe il resume "vero" di M2 (contratto §7.2) -- righe con
    next_action_at futuro restano ferme fino al loro appuntamento. E'
    accettabile per lo sblocco richiesto (le righe erano gia' in coda
    prima della pausa) ma non e' equivalente al resume completo di M2.

    Non flippa lo stato se il numero associato non e' 'active': il caso
    piu' comune di pausa (sessione WhatsApp caduta, cron
    wa_session_healthcheck) lascia il numero non-active finche' non si
    riscannerizza il QR -- rimettere la campagna a running in quel caso
    produrrebbe una campagna 'running' fantasma senza job attivo (il
    worker rifiuta l'invio su numero non-active e non ri-schedula).
    """
    campaign = await db.scalar(select(WaCampaign).where(WaCampaign.id == campaign_id))
    if campaign is None:
        raise HTTPException(status_code=404, detail="campagna inesistente")
    if campaign.status != WaCampaignStatus.paused:
        return {"resumed": False,
                "motivo": f"campagna in stato {campaign.status.value}, non paused"}
    numero = await db.scalar(select(WaNumber).where(WaNumber.id == campaign.wa_number_id))
    if numero is None or numero.status != WaNumberStatus.active:
        stato_numero = numero.status.value if numero else "inesistente"
        return {"resumed": False,
                "motivo": f"numero in stato {stato_numero}, non active"}
    campaign.status = WaCampaignStatus.running
    await db.commit()
    try:
        accodati = await enqueue_wa_workers(campaign_id)
    except Exception as exc:
        logger.error(f"[WA] resume_campaign {campaign_id}: stato gia' committato "
                     f"a running ma enqueue fallito: {exc!r}")
        return {"resumed": True, "accodati": 0, "errore_accodamento": str(exc)}
    return {"resumed": True, "accodati": accodati}


@router.post("/contacts/{contact_id}/revoke-optout")
async def revoke_optout(contact_id: str, body: RevokeOptoutRequest, db=Depends(get_db)) -> dict:
    """Revoca l'opt-out di un contatto (richiesto da Tommaso 27/07): oggi chi
    scrive STOP resta bloccato per sempre, senza modo di tornare indietro se
    poi cambia idea (es. richiama e chiede di essere ricontattato).

    Nota obbligatoria (validata dal body): revocare una preferenza di
    opt-out espressa dal contatto e' sensibile lato GDPR/ePrivacy, va sempre
    giustificato e loggato, mai silenzioso.

    Tocca SOLO wa_contacts, in avanti: le righe wa_campaign_contacts gia'
    terminali (opted_out) di campagne passate restano cosi' -- e' storico
    corretto, non si resuscitano invii su campagne gia' fermate. Il
    contratto M2/M3 (docs/whatsapp/contratto-M2-M3.md §7) ricontrolla
    l'opt-out live a ogni ingest: basta che il flag a livello contatto sia
    corretto perche' il contatto torni eleggibile per campagne FUTURE.

    Se il dnc_reason non e' 'optout' (es. 'manual', 'unreachable',
    'invalid_number' -- causa DNC indipendente, magari scritta da un altro
    processo dopo lo STOP), NON tocca do_not_contact/dnc_reason: quella
    causa resta valida a prescindere dalla revoca dell'opt-out.
    """
    contact = await db.scalar(select(WaContact).where(WaContact.id == contact_id))
    if contact is None:
        raise HTTPException(status_code=404, detail="contatto inesistente")

    if not contact.opted_out:
        return {"revoked": False, "motivo": "contatto non era in opt-out"}

    contact.opted_out = False
    contact.opted_out_at = None
    if contact.dnc_reason == WaDncReason.optout:
        contact.do_not_contact = False
        contact.dnc_reason = None
    await db.commit()

    logger.warning(
        f"[WA OPTOUT REVOKE] contatto={contact_id} nota={body.note[:60]!r}"
    )
    return {"revoked": True}
