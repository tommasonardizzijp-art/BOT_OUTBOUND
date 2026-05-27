"""Admin control-plane endpoints."""
from typing import Annotated
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.schemas.bot_state import BotHaltRequest, BotStateResponse
from app.services import bot_state_service
from app.services.work_enqueue import reenqueue_active_work
from app.utils.auth_deps import require_admin


router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/state", response_model=BotStateResponse)
async def get_admin_state(
    _: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    state = await bot_state_service.get_state(db)
    return BotStateResponse.model_validate(state)


@router.post("/halt", response_model=BotStateResponse)
async def halt_bot(
    data: BotHaltRequest,
    current_user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    await bot_state_service.halt(
        reason=data.reason,
        kind=data.kind or "manual_halt",
        by=current_user.email,
        db=db,
    )
    await db.commit()
    state = await bot_state_service.get_state(db)
    return BotStateResponse.model_validate(state)


@router.post("/resume", response_model=BotStateResponse)
async def resume_bot(
    current_user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    await bot_state_service.resume(by=current_user.email, db=db)
    await db.commit()
    await reenqueue_active_work()
    state = await bot_state_service.get_state(db)
    return BotStateResponse.model_validate(state)
