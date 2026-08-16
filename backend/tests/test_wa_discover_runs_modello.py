import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.wa import WaDiscoverRun
from tests.factories_wa import make_discover_run, make_number, make_tenant


@pytest.mark.asyncio
async def test_run_nasce_running_con_i_contatori_a_zero(db_session):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    run = await make_discover_run(db_session, tenant, number)
    await db_session.commit()

    letta = await db_session.scalar(select(WaDiscoverRun).where(WaDiscoverRun.id == run.id))
    assert letta.stato == "running"
    assert letta.avviato_da == "manuale"
    assert (letta.salvate, letta.aggiornate, letta.saltate_gia_note,
            letta.non_verificate) == (0, 0, 0, 0)
    assert letta.finished_at is None
    assert letta.sync_stato == "ignota"


@pytest.mark.asyncio
async def test_due_run_running_sullo_stesso_numero_sono_impossibili(db_session):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await make_discover_run(db_session, tenant, number)
    await db_session.commit()

    # Su SQLite/aiosqlite il vincolo UNIQUE si controlla all'INSERT (dentro il
    # flush della factory), non al commit -- stessa convenzione di
    # test_wa_messages_unique_step.py.
    with pytest.raises(IntegrityError):
        await make_discover_run(db_session, tenant, number)
    await db_session.rollback()


@pytest.mark.asyncio
async def test_due_run_chiuse_sullo_stesso_numero_convivono(db_session):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await make_discover_run(db_session, tenant, number, stato="done")
    await make_discover_run(db_session, tenant, number, stato="done")
    await db_session.commit()

    righe = (await db_session.execute(
        select(WaDiscoverRun).where(WaDiscoverRun.number_id == number.id))).scalars().all()
    assert len(righe) == 2
