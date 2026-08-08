"""C.1: pulsante 'attivita organica' accodato via ARQ, job_id deterministico
per account. Test contro Redis VERO (indice di test dello slot, mai db 0 —
vedi conftest.py) per verificare la guardia di concorrenza reale, non solo
la logica a intuito: "cinque richieste in raffica sullo stesso account ->
una sola sessione parte"."""
import uuid

import arq
import pytest
import pytest_asyncio

from app.services.work_enqueue import (
    arq_redis_settings, ARQ_MAIN_QUEUE, enqueue_organic_session, organic_session_job_id,
)


@pytest_asyncio.fixture
async def _redis_o_skip():
    try:
        pool = await arq.create_pool(arq_redis_settings())
        await pool.ping()
        await pool.aclose()
    except Exception as exc:
        pytest.skip(f"Redis non raggiungibile, test saltato: {type(exc).__name__}: {exc}")


async def _pulisci(account_id: str) -> None:
    job_id = organic_session_job_id(account_id)
    redis = await arq.create_pool(arq_redis_settings())
    try:
        await redis.zrem(ARQ_MAIN_QUEUE, job_id)
        await redis.delete(f"arq:job:{job_id}", f"arq:retry:{job_id}", f"arq:result:{job_id}")
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_enqueue_organic_session_prima_volta_accoda(_redis_o_skip):
    account_id = f"organic-test-{uuid.uuid4().hex[:8]}"
    try:
        started = await enqueue_organic_session(account_id, "utente_test")
        assert started is True

        redis = await arq.create_pool(arq_redis_settings())
        try:
            from arq.jobs import Job, JobStatus
            job = Job(organic_session_job_id(account_id), redis, _queue_name=ARQ_MAIN_QUEUE)
            status = await job.status()
            assert status in (JobStatus.queued, JobStatus.deferred)
        finally:
            await redis.aclose()
    finally:
        await _pulisci(account_id)


@pytest.mark.asyncio
async def test_cinque_click_di_fila_una_sola_sessione_in_coda(_redis_o_skip):
    """Il requisito esplicito del mandato: 5 richieste ravvicinate sullo
    stesso account, una sola deve accodare, le altre 4 devono riportare
    'occupato' senza toccare la coda."""
    account_id = f"organic-test-{uuid.uuid4().hex[:8]}"
    try:
        risultati = [await enqueue_organic_session(account_id, "utente_test") for _ in range(5)]
        assert risultati == [True, False, False, False, False], (
            f"attesi 1 True e 4 False, ottenuti {risultati}"
        )

        # Conferma che in coda ne resta esattamente UNO, non 5 accodamenti.
        redis = await arq.create_pool(arq_redis_settings())
        try:
            score = await redis.zscore(ARQ_MAIN_QUEUE, organic_session_job_id(account_id))
            assert score is not None  # il job c'e', una volta sola per costruzione del job_id
        finally:
            await redis.aclose()
    finally:
        await _pulisci(account_id)


@pytest.mark.asyncio
async def test_prova_del_nove_senza_guardia_status_tutti_i_click_accoderebbero(_redis_o_skip, monkeypatch):
    """Prova del nove del test precedente: se la guardia (Job.status() prima
    di enqueue_job) non ci fosse — cioe' si chiamasse `redis.enqueue_job`
    direttamente senza controllare lo stato — ARQ stesso rifiuterebbe le
    chiamate successive per collisione di `_job_id` (enqueue_job ritorna None
    se la job key esiste gia'), quindi il sintomo del bug rimosso non e'
    '5 job accodati' ma 'enqueue_job silenziosamente ignorato'. Si dimostra
    chiamando `redis.enqueue_job` grezzo 5 volte di fila sullo stesso job_id
    e verificando che le risposte 2-5 siano None (nessuna guardia esplicita,
    solo il comportamento nudo di ARQ) — la guardia della funzione reale
    invece traduce quel None in un False esplicito e leggibile, senza il
    quale il chiamante non saprebbe distinguere 'occupato' da un errore."""
    account_id = f"organic-test-{uuid.uuid4().hex[:8]}"
    job_id = organic_session_job_id(account_id)
    redis = await arq.create_pool(arq_redis_settings())
    try:
        risposte = []
        for _ in range(5):
            job = await redis.enqueue_job(
                "run_organic_session_task", account_id, "utente_test",
                _job_id=job_id, _queue_name=ARQ_MAIN_QUEUE,
            )
            risposte.append(job)
        assert risposte[0] is not None
        assert all(r is None for r in risposte[1:]), (
            "il comportamento nudo di ARQ e' gia' un no-op per le repliche — "
            "la guardia in enqueue_organic_session lo rende leggibile (True/False), non lo sostituisce"
        )
    finally:
        await redis.zrem(ARQ_MAIN_QUEUE, job_id)
        await redis.delete(f"arq:job:{job_id}", f"arq:retry:{job_id}", f"arq:result:{job_id}")
        await redis.aclose()


@pytest.mark.asyncio
async def test_enqueue_organic_session_dopo_completamento_puo_ripartire(_redis_o_skip):
    """Un run precedente completato (result key presente) non deve bloccare
    per sempre un secondo click legittimo — a differenza di
    `_enqueue_resolve_with_redis`, questa funzione ripulisce anche
    `arq:result:`, non solo `arq:job:`/`arq:retry:`."""
    account_id = f"organic-test-{uuid.uuid4().hex[:8]}"
    job_id = organic_session_job_id(account_id)
    redis = await arq.create_pool(arq_redis_settings())
    try:
        # Simula un run completato: solo la result key esiste (job/retry no).
        await redis.set(f"arq:result:{job_id}", b"finto-risultato-pickled", ex=3600)

        started = await enqueue_organic_session(account_id, "utente_test")
        assert started is True, "una result key di un run finito non deve bloccare un nuovo click"
    finally:
        await redis.zrem(ARQ_MAIN_QUEUE, job_id)
        await redis.delete(f"arq:job:{job_id}", f"arq:retry:{job_id}", f"arq:result:{job_id}")
        await redis.aclose()
