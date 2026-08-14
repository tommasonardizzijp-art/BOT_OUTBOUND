"""Job ARQ della scansione auto-discover.

Il browser vive nel worker come ogni altro browser di questo progetto: dentro
uvicorn non reggerebbe (su Windows --reload spegne Playwright, e ogni riavvio
perderebbe lo scan a meta' lasciando il lucchetto preso).
"""
from __future__ import annotations

import arq
from loguru import logger

from app.database import AsyncSessionLocal
from app.services import wa_discover_runs
from app.services.wa_discover_run import esegui_discover_run
from app.services.work_enqueue import arq_redis_settings


def wa_discover_job_id(run_id: str) -> str:
    """Un job per RUN, non per numero.

    wa_send_job_id lega l'id al number_id e conta sul fatto che ARQ scarti il
    duplicato -- li' e' voluto (max 1 campagna running per numero). Qui no: due
    scansioni successive sullo stesso numero sono due job legittimi, e con un
    id per-numero la seconda verrebbe scartata in silenzio (accodati 0, nessun
    errore) lasciando una run 'running' che non parte mai.
    """
    return f"wa:discover:{run_id}"


async def enqueue_wa_discover(number_id: str, run_id: str) -> bool:
    redis = await arq.create_pool(arq_redis_settings())
    try:
        job = await redis.enqueue_job("wa_discover_task", number_id, run_id,
                                      _job_id=wa_discover_job_id(run_id))
        return job is not None
    finally:
        await redis.aclose()


async def wa_discover_task(ctx: dict, number_id: str, run_id: str) -> None:
    """Esegue un giro di scan e chiude la run. Non solleva mai: un'eccezione
    che risalisse lascerebbe la run 'running' per sempre, e l'indice unico
    parziale renderebbe il numero non piu' scansionabile.
    """
    errore = None
    esito: dict = {}
    try:
        esito = await esegui_discover_run(number_id)
    except Exception as exc:  # noqa: BLE001 -- vedi docstring
        logger.exception(f"[WaDiscover] job {run_id} su {number_id}: {exc}")
        errore = f"{type(exc).__name__}: {exc}"

    async with AsyncSessionLocal() as db:
        await wa_discover_runs.chiudi_run(db, run_id, esito, errore=errore)
        await db.commit()

    logger.info(f"[WaDiscover] job {run_id} su {number_id} chiuso: "
                f"{esito.get('motivo', 'errore_imprevisto')}")
