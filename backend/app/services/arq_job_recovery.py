"""I job congelati da una chiave in-progress orfana. QUALUNQUE job, non solo WA.

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

PERCHE' IL PERIMETRO E' LA CODA E NON IL CANALE. La prima stesura (PR #96)
puliva il solo glob `wa:send:*`, e il nome del modulo diceva "wa": e' esatta-
mente per questo che i job Instagram sono rimasti scoperti mentre si congelano
con lo stesso identico meccanismo (16/08: due `biobrowser` fermi cosi'). Il
canale non c'entra nulla con la sicurezza dell'operazione. Cio' che la rende
sicura e' una cosa sola: che il job appartenga alla coda che QUESTO worker
consuma, perche' allora al suo avvio nessuno lo sta eseguendo. Da qui i due
controlli di `clear_orphan_in_progress_locks`, entrambi verificati sul sorgente
di arq installato e non dedotti:

1. il job deve avere uno score in ARQ_MAIN_QUEUE. ARQ toglie il job dal sorted
   set solo in `finish_job`/`finish_failed_job` (`tr.zrem(self.queue_name, ...)`,
   arq/worker.py:702 e :717), quindi un job congelato ce l'ha sempre -- e' la
   definizione stessa del congelamento (score presente e gia' scaduto). I job
   del cron worker invece vengono accodati con `_queue_name=self.queue_name`
   (arq/worker.py:759), cioe' su ARQ_CRON_QUEUE: uno `zscore` su `arq:queue`
   non li trova MAI. Il controllo si adatta da solo ai job futuri: un prefisso
   nuovo sulla coda principale e' coperto senza toccare questo file, che e'
   proprio l'errore che si sta correggendo.
2. il job id non deve cominciare per `cron:` (arq/cron.py:173, `name = name or
   'cron:' + coroutine`). E' ridondante rispetto al primo per com'e' schierato
   oggi il sistema, ed e' voluto: se un domani il cron worker condividesse la
   coda principale, il controllo 1 da solo lo esporrebbe a rieseguire un cron
   mentre e' in corso (un secondo Chromium sullo stesso profilo, reply-scan
   doppio). Due condizioni indipendenti, entrambe necessarie.
"""
import time

from loguru import logger

IN_PROGRESS_PREFIX = "arq:in-progress:"

# arq/cron.py:173 -- il nome di un cron job, e quindi il suo job_id, nasce come
# `cron:{funzione}`. Ancorato in test: se arq lo cambiasse, la denylist
# diventerebbe muta invece che rossa.
CRON_JOB_PREFIX = "cron:"

ORPHAN_SCAN_PATTERN = f"{IN_PROGRESS_PREFIX}*"


async def clear_orphan_in_progress_locks(redis) -> list[str]:
    """Cancella le chiavi in-progress rimaste appese ai job della coda
    principale, e ritorna i job id ripuliti. Da chiamare SOLO all'avvio del
    worker principale.

    Copre tutti i canali: invio WhatsApp (`wa:send:`), discover (`wa:discover:`)
    e i job Instagram (`worker:`, `biobrowser:`, `importbrowser:`, `scrape:`,
    `list:`, `bios:`, `resolve:`, `organic-session:`, `pregen:`,
    `lead-qualification:`). Non per elenco -- vedi il docstring del modulo --
    ma perche' il criterio e' l'appartenenza alla coda.

    RISCHIO RESIDUO ACCETTATO (dichiarato, non mitigato qui): se esistesse una
    SECONDA istanza del worker principale che sta davvero lavorando, cancellarle
    la chiave lascerebbe ripescare il suo job -> una seconda sessione sullo
    stesso account. E' contenuto dalle protezioni gia' esistenti -- lucchetto di
    profilo browser per numero/account, claim per contatto (`locked_by`), indice
    unico parziale su wa_messages -- e il deploy e' a worker singolo. Rispetto
    alla PR #96 la superficie di questo rischio si allarga da un canale a tutti,
    e resta accettata alle stesse condizioni: si dichiara e basta.

    SCAN e non KEYS: KEYS blocca il server per tutta la scansione del keyspace,
    e qui gira all'avvio, quando la coda ha gia' lavoro in attesa.
    """
    from app.services.work_enqueue import ARQ_MAIN_QUEUE

    chiavi: list[str] = []
    saltati: list[str] = []
    async for chiave in redis.scan_iter(match=ORPHAN_SCAN_PATTERN, count=100):
        chiave = chiave.decode() if isinstance(chiave, bytes) else chiave
        job_id = chiave[len(IN_PROGRESS_PREFIX):]

        if job_id.startswith(CRON_JOB_PREFIX):
            saltati.append(job_id)
            continue
        if await redis.zscore(ARQ_MAIN_QUEUE, job_id) is None:
            # Non e' in coda qui: o e' di un'altra coda, o e' una chiave che non
            # blocca nessuno. In entrambi i casi cancellarla non sbloccherebbe
            # nulla e potrebbe far ripartire il lavoro di un altro processo.
            saltati.append(job_id)
            continue
        chiavi.append(chiave)

    if saltati:
        logger.info(f"[Startup] {len(saltati)} chiavi in-progress lasciate "
                    f"intatte perche' fuori dalla coda principale: "
                    f"{', '.join(saltati)}")

    if not chiavi:
        # Il caso NORMALE (avvio con Redis pulito). Serve uscire prima: redis-py
        # solleva su un DELETE senza argomenti, e questa funzione gira a ogni
        # startup del worker di produzione.
        return []

    await redis.delete(*chiavi)
    job_ids = [c[len(IN_PROGRESS_PREFIX):] for c in chiavi]
    logger.warning(
        f"[Startup] {len(job_ids)} chiavi in-progress orfane cancellate "
        f"({', '.join(job_ids)}) -- i job corrispondenti erano bloccati "
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
