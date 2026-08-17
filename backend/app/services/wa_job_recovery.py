"""Il job di invio WhatsApp congelato da una chiave in-progress orfana.

Quando ARQ prende in carico un job scrive `arq:in-progress:{job_id}` e la
cancella solo in `finish_job` (arq/worker.py). Se il processo muore mentre il
job e' checked-out -- un riavvio del worker a meta' mini-sessione -- quella
chiave resta orfana, e `start_jobs` SALTA qualunque job in coda che abbia la
sua chiave in-progress:

    if ongoing_exists or not score or score > timestamp_ms(): continue

Il job resta in `arq:queue` con lo score gia' scaduto e nessuno lo esegue fino
alla scadenza del TTL, in silenzio: nessun errore, da nessuna parte (incidente
14/08: campagna running, 48 contatti eleggibili, zero invii per ore).

QUEL TTL E' DI SEI ORE, NON DI UNA. Non vale `job_timeout + 10`: ARQ ne tiene
UNO SOLO per tutto il worker, ed e' il MASSIMO fra i timeout di TUTTE le
funzioni registrate (arq/worker.py:272-273):

    max_timeout = max(f.timeout_s or self.job_timeout_s for f in self.functions.values())
    self.in_progress_timeout_s = (max_timeout or 0) + 10

Da quando `task_queue.py` registra `func(wa_discover_task, timeout=21600)` --
Task 11, che serviva a non far uccidere dal timeout uno scan lungo -- quel
massimo e' 21600, quindi il TTL di OGNI job di questo worker e' 21610s = 6 ore.
Un accorgimento pensato per il discover ha sestuplicato in silenzio la durata
dei congelamenti dell'invio. Riscontro dal campo, indipendente dalla lettura
del codice: il 17/08 due chiavi in-progress lasciate orfane il giorno prima
avevano `ttl` 21132 e 21413.

Conta perche' chiude la scorciatoia "tanto si sana da sola in un'ora": sei ore
di invii persi sono una giornata di campagna. Ed e' anche l'unico motivo per
cui l'allarme del supervisore ha una finestra utile: col ritardo strutturale
del predicato (30-55 min, vedi `wa_send_job_congelato`) a TTL di un'ora
avrebbe suonato quasi solo dopo l'auto-guarigione.

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

    ⚠️ Score scaduto + in-progress NON bastano. ARQ toglie il job dal sorted set
    solo in `finish_job` (arq/worker.py: `tr.zrem(self.queue_name, job_id)`),
    quindi per TUTTA la durata di una mini-sessione normale -- 8-17 minuti, e il
    supervisore gira ogni 15 -- lo stato a Redis e' identico a quello di un
    congelamento. Senza il terzo controllo l'allarme suonerebbe a ogni giro
    mentre il canale invia benissimo, cioe' insegnerebbe a ignorarlo.

    Il terzo controllo e' il lucchetto di profilo (`wa:profile-lock:{number_id}`),
    l'unico segnale che dice "qualcuno sta DAVVERO lavorando su questo numero":
    esegui_mini_sessione lo tiene per tutta la sessione e lo rinnova a ogni
    messaggio. Si riusa quella disciplina invece di introdurre una soglia nuova.

    PREZZO DEL TERZO CONTROLLO: 30-55 MINUTI DI SILENZIO. Quando il worker
    muore a meta' sessione restano orfane DUE chiavi, non una: la in-progress e
    anche `wa:profile-lock:{number_id}`. Finche' c'e' il lucchetto questa
    funzione dice "sano". Il lucchetto sparisce solo con
    release_stale_wa_profile_locks (cron :05/:20/:35/:50, soglia
    wa_profile_lock_stale_min=25 min) -> fino a 40 min; poi il supervisore gira
    a :10/:25/:40/:55 -> altri 15. Con un TTL di sei ore resta comunque una
    finestra utile ampia, ma il ritardo va detto giusto: non e' "al piu' una
    mezz'ora".

    Due percorsi in cui tace pur essendo congelato, entrambi transitori:
    - il lucchetto NON e' esclusivo dell'invio. Lo prendono sullo stesso
      number_id anche l'health-check di sessione (apre un Chromium, minuti) e
      il reply-scan: uno di questi in corso zittisce il tick del supervisore
      anche su un job genuinamente congelato. Il tick dopo lo vede.
    - fra l'inizio del job e l'acquisizione del lucchetto ci sono i cancelli di
      precheck (pochi secondi): un tick esattamente li' darebbe un falso
      allarme. Secondi contro minuti, si accetta.
    """
    from app.services import wa_profile_lock
    from app.services.work_enqueue import ARQ_MAIN_QUEUE
    from app.workers.wa_worker import wa_send_job_id

    job_id = wa_send_job_id(number_id)
    score = await redis.zscore(ARQ_MAIN_QUEUE, job_id)
    if score is None or score > time.time() * 1000:
        return None
    if not await redis.exists(f"{IN_PROGRESS_PREFIX}{job_id}"):
        return None
    if await redis.exists(wa_profile_lock.lock_key(number_id)):
        return None
    return job_id
