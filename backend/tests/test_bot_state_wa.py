import pytest

from app.services import bot_state_service as bss


@pytest.mark.asyncio
async def test_halt_ig_non_ferma_wa(db_session):
    """Non-regressione IG + isolamento dei due canali: halt() e' il
    kill-switch Instagram e NON deve toccare wa_halted."""
    await bss.halt(reason="test IG", by="pytest", db=db_session)
    assert await bss.is_halted(db_session) is True
    assert await bss.is_wa_halted(db_session) is False
    await bss.resume(by="pytest", db=db_session)
    assert await bss.is_halted(db_session) is False


@pytest.mark.asyncio
async def test_halt_wa_non_ferma_ig(db_session):
    await bss.halt_wa(reason="test WA", by="pytest", db=db_session)
    assert await bss.is_wa_halted(db_session) is True
    assert await bss.is_halted(db_session) is False
    await bss.resume_wa(by="pytest", db=db_session)
    assert await bss.is_wa_halted(db_session) is False


@pytest.mark.asyncio
async def test_is_wa_halted_su_riga_assente_torna_false_e_non_solleva(db_session):
    """Fail-safe di lettura: se la riga singleton non esiste ancora,
    is_wa_halted deve rispondere False (nessun blocco fantasma), non
    esplodere dentro il check di un worker."""
    from sqlalchemy import delete
    from app.models.bot_state import BotState
    await db_session.execute(delete(BotState))
    await db_session.commit()
    assert await bss.is_wa_halted(db_session) is False
