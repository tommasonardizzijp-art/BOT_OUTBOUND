"""Il job di invio WhatsApp congelato da una chiave in-progress orfana.

Quando ARQ prende in carico un job scrive `arq:in-progress:{job_id}` con TTL
`job_timeout + 10` (qui job_timeout=3600 -> 3610s) e la cancella solo in
`finish_job` (arq/worker.py). Se il processo muore mentre il job e' checked-out
-- un riavvio del worker a meta' mini-sessione -- quella chiave resta orfana, e
`start_jobs` SALTA qualunque job in coda che abbia la sua chiave in-progress:

    if ongoing_exists or not score or score > timestamp_ms(): continue

Il job resta in `arq:queue` con lo score gia' scaduto e nessuno lo esegue fino
alla scadenza del TTL: fino a un'ora di invii persi a ogni riavvio fatto mentre
il worker lavorava, in silenzio (nessun errore, da nessuna parte -- incidente
14/08: campagna running, 48 contatti eleggibili, zero invii per ore).

Il TTL NON viene rinnovato durante l'esecuzione, quindi un pttl che cala non
distingue un job vivo da una chiave orfana: l'unico momento in cui la
distinzione e' certa e' l'avvio del worker principale, dove per definizione
nessun job di QUESTO processo e' ancora in corso. E' li' che gira la pulizia.
"""
import time

from loguru import logger

IN_PROGRESS_PREFIX = "arq:in-progress:"

# ⚠️ Il glob deve restare `wa:send:*`, MAI `arq:in-progress:*`.
# Il cron worker gira in un processo SEPARATO che non si riavvia insieme a
# questo, consuma una coda diversa (ARQ_CRON_QUEUE) e le sue chiavi sono
# `arq:in-progress:cron:*`: cancellargliele significherebbe far ripartire un
# cron mentre e' ancora in corso (health-check con un secondo Chromium sullo
# stesso profilo, reply-scan doppio). Stesso discorso per i job Instagram
# (`worker:`, `scrape:`, `list:`, `bios:`, ...), che qui non c'entrano nulla.
ORPHAN_WA_SEND_PATTERN = f"{IN_PROGRESS_PREFIX}wa:send:*"


async def clear_orphan_wa_send_locks(redis) -> list[str]:
    """Cancella le chiavi in-progress dei job di invio WA rimaste appese, e
    ritorna i job id ripuliti. Da chiamare SOLO all'avvio del worker principale.

    RISCHIO RESIDUO ACCETTATO (dichiarato, non mitigato qui): se esistesse una
    SECONDA istanza del worker principale che sta davvero inviando, cancellarle
    la chiave lascerebbe ripescare il suo job -> una seconda mini-sessione sullo
    stesso numero. E' contenuto dalle protezioni gia' esistenti -- lucchetto di
    profilo per numero (wa_profile_lock), claim per contatto (`locked_by`),
    indice unico parziale su wa_messages -- e il deploy e' a worker singolo.
    Non si aggiungono altre protezioni: si dichiara e basta.

    SCAN e non KEYS: KEYS blocca il server per tutta la scansione del keyspace,
    e qui gira all'avvio, quando la coda ha gia' lavoro in attesa.
    """
    chiavi: list[str] = []
    async for chiave in redis.scan_iter(match=ORPHAN_WA_SEND_PATTERN, count=100):
        chiavi.append(chiave.decode() if isinstance(chiave, bytes) else chiave)

    if not chiavi:
        # Il caso NORMALE (avvio con Redis pulito). Serve uscire prima: redis-py
        # solleva su un DELETE senza argomenti, e questa funzione gira a ogni
        # startup del worker di produzione.
        return []

    await redis.delete(*chiavi)
    job_ids = [c[len(IN_PROGRESS_PREFIX):] for c in chiavi]
    logger.warning(
        f"[Startup] WA: {len(job_ids)} chiavi in-progress orfane cancellate "
        f"({', '.join(job_ids)}) -- i job di invio corrispondenti erano bloccati "
        "in coda e ripartiranno al prossimo poll"
    )
    return job_ids


async def wa_send_job_congelato(redis, number_id: str) -> str | None:
    """Ritorna il job id se il job di invio del numero e' CONGELATO, None se e'
    sano. Serve al supervisore per distinguere due situazioni che dall'esterno
    sono identiche (enqueue scartato da ARQ perche' il job esiste gia'):

    - schedulato nel FUTURO -> break anti-ban fra due mini-sessioni: normale;
    - in coda con lo score GIA' SCADUTO e la chiave in-progress presente ->
      nessuno lo eseguira' mai finche' il TTL non scade.

    Score scaduto ma senza chiave in-progress non e' un congelamento: e' un job
    che il worker raccogliera' al prossimo poll (mezzo secondo).
    """
    from app.services.work_enqueue import ARQ_MAIN_QUEUE
    from app.workers.wa_worker import wa_send_job_id

    job_id = wa_send_job_id(number_id)
    score = await redis.zscore(ARQ_MAIN_QUEUE, job_id)
    if score is None or score > time.time() * 1000:
        return None
    if not await redis.exists(f"{IN_PROGRESS_PREFIX}{job_id}"):
        return None
    return job_id
