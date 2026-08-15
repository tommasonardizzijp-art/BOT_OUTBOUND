"""Job ARQ della scansione auto-discover.

Il browser vive nel worker come ogni altro browser di questo progetto: dentro
uvicorn non reggerebbe (su Windows --reload spegne Playwright, e ogni riavvio
perderebbe lo scan a meta' lasciando il lucchetto preso).
"""
from __future__ import annotations

import asyncio

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
    """Esegue un giro di scan e chiude la run. Non solleva MAI di suo -- un
    Exception che risalisse da qui lascerebbe la run 'running' per sempre, e
    l'indice unico parziale renderebbe il numero non piu' scansionabile.

    ECCEZIONE (letteralmente): asyncio.CancelledError, quando ARQ cancella
    il job al proprio timeout (Task 11). Quella la run la chiude comunque,
    ma poi la rilancia sempre -- ingoiarla farebbe credere ad ARQ che il job
    e' finito con successo invece che ucciso.

    Il secondo try copre la chiusura/commit, non solo il motore: un blip del
    DB proprio li' (scenario reale, non teorico -- un TimeoutError di
    asyncpg sul pooler Supabase e' gia' successo) non deve propagare. Se
    anche quel tentativo fallisce, un secondo giro "a mani nude" (sessione
    nuova, chiudi_run di nuovo, con l'errore del primo fallimento come testo)
    prova a marcare la run failed comunque -- e quel tentativo e' avvolto a
    sua volta, perche' non ha nessuno sopra di se' a cui appoggiarsi: se
    fallisce anche lui, si logga e basta, la run puo' restare 'running' e
    serve intervento manuale.
    """
    errore = None
    esito: dict = {}
    try:
        esito = await esegui_discover_run(number_id)
    except asyncio.CancelledError:
        # ARQ cancella il job al proprio timeout (task_queue.py, Task 11):
        # CancelledError eredita da BaseException, non da Exception, quindi
        # il blanket except sotto non la vede mai. Motivo DEDICATO, non
        # 'errore_imprevisto': un timeout non e' un guasto del motore, e'
        # il job che ha superato il tempo che gli abbiamo dato. 'cancellato'
        # non e' in MOTIVI_NON_GUASTO di proposito -- e' un guasto vero,
        # l'unica traccia che uno scan e' morto a meta'.
        logger.error(f"[WaDiscover] job {run_id} su {number_id}: cancellato "
                     "da ARQ (timeout del job)")
        try:
            async with AsyncSessionLocal() as db:
                await wa_discover_runs.chiudi_run(db, run_id, {"motivo": "cancellato"})
                await db.commit()
        except Exception as exc2:  # noqa: BLE001 -- ultimo cancello, vedi sotto
            logger.error(
                f"[WaDiscover] job {run_id} su {number_id}: chiusura dopo "
                f"cancellazione fallita ({type(exc2).__name__}: {exc2}) -- la "
                "run puo' restare 'running', serve intervento manuale")
        # MAI ingoiare una CancelledError: ARQ deve poterla vedere per sapere
        # che il job e' morto, non completato con successo.
        raise
    except Exception as exc:  # noqa: BLE001 -- vedi docstring
        logger.exception(f"[WaDiscover] job {run_id} su {number_id}: {exc}")
        errore = f"{type(exc).__name__}: {exc}"

    try:
        async with AsyncSessionLocal() as db:
            await wa_discover_runs.chiudi_run(db, run_id, esito, errore=errore)
            await db.commit()
    except Exception as exc:  # noqa: BLE001 -- vedi docstring
        logger.error(f"[WaDiscover] job {run_id} su {number_id}: chiusura "
                     f"fallita ({type(exc).__name__}: {exc}), riprovo a mani nude")
        try:
            async with AsyncSessionLocal() as db:
                await wa_discover_runs.chiudi_run(
                    db, run_id, {},
                    errore=f"chiusura fallita: {type(exc).__name__}: {exc}")
                await db.commit()
        except Exception as exc2:  # noqa: BLE001 -- ultimo cancello, vedi docstring
            logger.error(
                f"[WaDiscover] job {run_id} su {number_id}: anche il tentativo "
                f"a mani nude e' fallito ({type(exc2).__name__}: {exc2}) -- la "
                "run puo' restare 'running', serve intervento manuale")
        return

    logger.info(f"[WaDiscover] job {run_id} su {number_id} chiuso: "
                f"{esito.get('motivo', 'errore_imprevisto')}")
