"""Operativita' del canale WhatsApp: kill-switch, stato, avvio manuale.

M3 e' backend puro (decisione 29/07): non c'e' UI in questo modulo. Le
pagine, quando arriveranno, le costruisce M2 contro questi endpoint.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from app.database import get_db
from app.models.wa import (WaCampaign, WaCampaignStatus, WaMessage,
                           WaMessageStatus, WaNumber, WaNumberStatus)
from app.services import bot_state_service
from app.workers.wa_worker import enqueue_wa_workers

router = APIRouter(prefix="/wa/ops", tags=["wa-ops"])


class HaltRequest(BaseModel):
    reason: str


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
