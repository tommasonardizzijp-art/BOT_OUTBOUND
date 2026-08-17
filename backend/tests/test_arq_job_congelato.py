"""Il job di invio WA congelato da una chiave in-progress orfana.

Incidente 14/08: campagna running, 48 contatti eleggibili, zero invii per ore,
nessun errore da nessuna parte. ARQ scrive `arq:in-progress:{job_id}` quando
prende in carico un job e la cancella solo in finish_job: un riavvio del worker
a meta' mini-sessione lascia la chiave orfana, e `start_jobs` salta per sempre
un job che ha la sua chiave in-progress (arq/worker.py, la guardia
`if ongoing_exists or not score or score > timestamp_ms(): continue`). Il job
resta in `arq:queue` con lo score gia' scaduto e nessuno lo esegue fino alla
scadenza del TTL: SEI ore di invii persi per ogni riavvio fatto mentre il
worker lavorava. Non e' job_timeout+10 -- ARQ tiene un solo TTL per worker,
pari al MASSIMO fra i timeout di tutte le funzioni registrate, e da quando il
discover e' registrato con timeout=21600 quel massimo vale per tutti
(dettaglio e prove in app/services/arq_job_recovery.py).

Due difese, provate qui: la pulizia all'avvio del worker principale (che
rimuove la causa) e l'allarme del supervisore (che rende visibile il
congelamento invece di contare "0 riaccodate" e tacere)."""
from datetime import timedelta

import fakeredis.aioredis
import pytest

from app.utils.tempo import adesso_utc
from tests.factories_wa import (make_campaign, make_campaign_contact,
                                make_contact, make_number, make_tenant)


@pytest.fixture
def fake_redis():
    """Stesso doppio di test_wa_profile_lock.py / test_inbox_segnalibro_redis.py:
    un Redis vero non serve, e queste funzioni ricevono il pool dal chiamante."""
    return fakeredis.aioredis.FakeRedis()


# ---------------------------------------------------------------------------
# Pulizia delle chiavi in-progress orfane (all'avvio del worker principale)
# ---------------------------------------------------------------------------

async def _congela(fake_redis, job_id: str) -> str:
    """Mette a Redis lo stato ESATTO di un job congelato: score in coda gia'
    scaduto (ARQ toglie il job dal sorted set solo in finish_job) e chiave
    in-progress presente. Scriverne solo una delle due farebbe passare i test
    per il motivo sbagliato."""
    import time

    await fake_redis.zadd("arq:queue", {job_id: time.time() * 1000 - 60_000})
    await fake_redis.set(f"arq:in-progress:{job_id}", b"1")
    return job_id


@pytest.mark.asyncio
async def test_pulizia_copre_tutti_i_canali_non_solo_l_invio_wa(fake_redis):
    """Il test che descrive il difetto corretto qui. La PR #96 puliva il solo
    glob `wa:send:*`, e i job Instagram -- che si congelano con lo stesso
    identico meccanismo -- restavano fermi sei ore (16/08: due `biobrowser`).

    I job id NON sono scritti a mano: vengono dai costruttori veri, cosi' un
    cambio di formato diventa rosso qui invece che invisibile in produzione."""
    from app.services.arq_job_recovery import clear_orphan_in_progress_locks
    from app.services.browser_bio import browser_bio_job_id
    from app.services.browser_import import browser_import_job_id
    from app.services.work_enqueue import dm_worker_job_id
    from app.workers.wa_discover_worker import wa_discover_job_id
    from app.workers.wa_worker import wa_send_job_id

    attesi = [
        await _congela(fake_redis, wa_send_job_id("num-1")),
        await _congela(fake_redis, wa_discover_job_id("run-1")),
        await _congela(fake_redis, browser_bio_job_id("camp-1", "acc-1")),
        await _congela(fake_redis, browser_import_job_id("camp-1", "acc-2")),
        await _congela(fake_redis, dm_worker_job_id("camp-1", "acc-3")),
        await _congela(fake_redis, "scrape:camp-1"),
        await _congela(fake_redis, "list:camp-1"),
        await _congela(fake_redis, "bios:camp-1"),
        await _congela(fake_redis, "resolve:camp-1"),
        await _congela(fake_redis, "organic-session:acc-4"),
        await _congela(fake_redis, "pregen:camp-1:full"),
        await _congela(fake_redis, "lead-qualification:run-2"),
    ]

    ripuliti = await clear_orphan_in_progress_locks(fake_redis)

    assert sorted(ripuliti) == sorted(attesi), (
        "un canale e' rimasto congelato: la pulizia non copre tutta la coda "
        "principale")
    for job_id in attesi:
        assert not await fake_redis.exists(f"arq:in-progress:{job_id}")


@pytest.mark.asyncio
async def test_pulizia_non_tocca_le_chiavi_del_cron_worker(fake_redis):
    """Il test che protegge dal danno peggiore, e l'unico motivo per cui la
    denylist su `cron:` esiste ancora accanto al controllo sulla coda.

    Il cron worker gira in un processo SEPARATO che non si riavvia insieme a
    questo: rieseguire un cron in corso significa un secondo Chromium sullo
    stesso profilo (health-check) o un reply-scan doppio. Oggi le sue chiavi
    sono al riparo gia' per la coda -- accoda su ARQ_CRON_QUEUE, quindi nessuno
    score in `arq:queue`. La seconda riga di questo test e' il caso IPOTETICO in
    cui condividesse la coda: deve restare intatta lo stesso."""
    from app.services.arq_job_recovery import clear_orphan_in_progress_locks

    await fake_redis.set("arq:in-progress:cron:wa_session_healthcheck:456", b"1")
    condivisa = await _congela(fake_redis, "cron:wa_reply_scan:789")

    ripuliti = await clear_orphan_in_progress_locks(fake_redis)

    assert ripuliti == []
    assert await fake_redis.exists("arq:in-progress:cron:wa_session_healthcheck:456")
    assert await fake_redis.exists(f"arq:in-progress:{condivisa}"), (
        "una chiave del cron con lo score in coda principale e' stata "
        "cancellata: resta solo il controllo sulla coda, la denylist non tiene")


@pytest.mark.asyncio
async def test_pulizia_non_tocca_un_job_che_non_e_in_coda_qui(fake_redis):
    """Chiave in-progress senza score in `arq:queue`: cancellarla non
    sbloccherebbe niente (non c'e' nessun job fermo da liberare) e potrebbe
    invece far ripescare il lavoro di un altro processo. Si lascia stare."""
    from app.services.arq_job_recovery import clear_orphan_in_progress_locks

    await fake_redis.set("arq:in-progress:worker:camp-9:acc-9", b"1")

    ripuliti = await clear_orphan_in_progress_locks(fake_redis)

    assert ripuliti == []
    assert await fake_redis.exists("arq:in-progress:worker:camp-9:acc-9")


@pytest.mark.asyncio
async def test_pulizia_non_tocca_le_chiavi_di_servizio_del_job(fake_redis):
    """Sparisce SOLO la in-progress. Senza `arq:job:{id}` il job non esiste
    piu' e non lo raccoglie nessuno; il lucchetto di profilo non e' roba di ARQ
    e ha la sua scadenza."""
    from app.services.arq_job_recovery import clear_orphan_in_progress_locks

    job_id = await _congela(fake_redis, "wa:send:num-1")
    intatte = (f"arq:job:{job_id}", f"arq:retry:{job_id}", "wa:profile-lock:num-1")
    for chiave in intatte:
        await fake_redis.set(chiave, b"1")

    ripuliti = await clear_orphan_in_progress_locks(fake_redis)

    assert ripuliti == [job_id]
    for chiave in intatte:
        assert await fake_redis.exists(chiave), f"{chiave} non doveva essere toccata"
    assert await fake_redis.zscore("arq:queue", job_id) is not None, (
        "il job e' stato tolto dalla coda: cosi' non riparte, resta perso")


@pytest.mark.asyncio
async def test_uno_score_zero_non_viene_scambiato_per_assente(fake_redis):
    """`zscore` ritorna 0.0 per un job accodato all'epoch, ed e' un valore
    FALSY. Un `if not score` lascerebbe quella chiave in piedi per sempre --
    ed e' un errore vivo: e' esattamente cio' che fa arq alla guardia di
    start_jobs (`if ongoing_exists or not score or ...`). Qui si distingue
    "score assente" da "score zero"."""
    from app.services.arq_job_recovery import clear_orphan_in_progress_locks

    await fake_redis.zadd("arq:queue", {"wa:send:num-0": 0})
    await fake_redis.set("arq:in-progress:wa:send:num-0", b"1")

    ripuliti = await clear_orphan_in_progress_locks(fake_redis)

    assert ripuliti == ["wa:send:num-0"], (
        "uno score 0.0 e' stato letto come 'non in coda': la chiave resta "
        "orfana e il job non riparte mai")


@pytest.mark.asyncio
async def test_anche_un_job_schedulato_nel_futuro_va_sbloccato(fake_redis):
    """Score futuro + in-progress presente NON e' un break anti-ban sano: la
    guardia di arq salta il job per `ongoing_exists` anche quando l'ora arriva,
    quindi resterebbe fermo lo stesso. All'avvio del worker nessun suo job puo'
    essere davvero in corso, quindi quella chiave e' orfana per costruzione.

    (Il supervisore, che gira a worker VIVO, fa la scelta opposta e tace: la'
    lo score futuro e' informazione buona. Due contesti, due predicati.)"""
    import time

    from app.services.arq_job_recovery import clear_orphan_in_progress_locks

    await fake_redis.zadd("arq:queue", {"wa:send:num-f": time.time() * 1000 + 900_000})
    await fake_redis.set("arq:in-progress:wa:send:num-f", b"1")

    assert await clear_orphan_in_progress_locks(fake_redis) == ["wa:send:num-f"]


@pytest.mark.asyncio
async def test_una_chiave_malformata_non_ferma_la_pulizia_delle_altre(fake_redis):
    """Chiave senza job id e job id con separatori strani: non devono sollevare
    ne' far saltare il resto. La pulizia gira dentro un try in `on_startup`, ma
    quel try e' la rete -- se scatta, tutte le chiavi successive restano non
    esaminate e l'avvio sembra riuscito."""
    from app.services.arq_job_recovery import clear_orphan_in_progress_locks

    await fake_redis.set("arq:in-progress:", b"1")            # job id vuoto
    await fake_redis.set("arq:in-progress:::::", b"1")        # solo separatori
    buona = await _congela(fake_redis, "biobrowser:camp-1:acc-1")

    ripuliti = await clear_orphan_in_progress_locks(fake_redis)

    assert ripuliti == [buona], (
        "una chiave malformata ha fatto saltare la pulizia delle altre")
    assert await fake_redis.exists("arq:in-progress:")


@pytest.mark.asyncio
async def test_molte_chiavi_orfane_una_sola_delete(fake_redis, monkeypatch):
    """La pulizia gira all'avvio, quando la coda ha gia' lavoro in attesa: le
    cancellazioni devono partire in UN comando, non una per chiave."""
    from app.services import arq_job_recovery

    for i in range(200):
        await _congela(fake_redis, f"biobrowser:camp-1:acc-{i}")

    delete_originale = fake_redis.delete
    chiamate = []

    async def _delete_spia(*chiavi):
        chiamate.append(len(chiavi))
        return await delete_originale(*chiavi)
    monkeypatch.setattr(fake_redis, "delete", _delete_spia)

    ripuliti = await arq_job_recovery.clear_orphan_in_progress_locks(fake_redis)

    assert len(ripuliti) == 200
    assert chiamate == [200], f"{len(chiamate)} DELETE invece di uno solo"


@pytest.mark.asyncio
async def test_pulizia_senza_chiavi_orfane_non_tocca_nulla(fake_redis):
    """Il caso NORMALE (avvio con Redis pulito) non deve ne' cancellare niente
    ne' sollevare: la pulizia gira a ogni startup del worker di produzione, e
    redis-py solleva su un DELETE senza argomenti."""
    from app.services.arq_job_recovery import clear_orphan_in_progress_locks

    await fake_redis.set("arq:in-progress:cron:daily_reset:1", b"1")

    ripuliti = await clear_orphan_in_progress_locks(fake_redis)

    assert ripuliti == []
    assert await fake_redis.exists("arq:in-progress:cron:daily_reset:1")


def test_on_startup_chiama_la_pulizia_delle_chiavi_orfane(monkeypatch):
    """La funzione giusta non cablata in produzione e' gia' successo una volta
    su questo stesso `on_startup` (FM14, recovery WA scritta e testata ma mai
    chiamata). Qui si prova il collegamento, non la logica: deve girare a ogni
    avvio del worker principale, in aggiunta -- non al posto -- delle due
    recovery gia' presenti."""
    import asyncio

    from app.workers import task_queue

    chiamate = {"pulizia": None, "wa": False, "ig": False}
    redis_finto = object()

    async def _fake_pulizia(redis):
        chiamate["pulizia"] = redis
        return []
    monkeypatch.setattr(task_queue, "clear_orphan_in_progress_locks", _fake_pulizia)

    async def _fake_wa():
        chiamate["wa"] = True
        return 0
    monkeypatch.setattr(task_queue, "recover_wa_sending_on_startup", _fake_wa)

    async def _fake_ig():
        chiamate["ig"] = True
        return {"campaigns_paused": 0, "locks_released": 0, "leases_released": 0}
    monkeypatch.setattr("app.services.work_enqueue.pause_active_work_on_startup", _fake_ig)

    asyncio.run(task_queue.on_startup({"redis": redis_finto}))

    assert chiamate["pulizia"] is redis_finto
    assert chiamate["wa"] is True and chiamate["ig"] is True


# ---------------------------------------------------------------------------
# Il supervisore distingue il job congelato dal break normale
# ---------------------------------------------------------------------------

async def _scenario_eleggibile(db_session):
    """Campagna running con una riga pronta ADESSO: il supervisore la vede
    eleggibile e prova a riaccodare."""
    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    from app.models.wa import WaCampaignStatus
    campagna, _step = await make_campaign(db_session, tenant, numero,
                                          status=WaCampaignStatus.running)
    cc = await make_campaign_contact(db_session, campagna, contatto)
    cc.next_action_at = adesso_utc() - timedelta(minutes=1)
    await db_session.commit()
    return {"numero": numero, "campagna": campagna, "cc": cc}


@pytest.fixture
def _enqueue_scartato(monkeypatch):
    """ARQ ha scartato il duplicato: il job esiste gia'. E' esattamente cio' che
    il supervisore vedeva nell'incidente -- e che contava come "0 riaccodate",
    cioe' come tutto a posto."""
    async def _finta(campaign_id: str, **kw) -> int:
        return 0
    monkeypatch.setattr("app.workers.wa_worker.enqueue_wa_workers", _finta)


@pytest.fixture
def _telegram_spia(monkeypatch):
    inviati: list[tuple[str, str]] = []

    async def _finto(msg, level="info"):
        inviati.append((msg, level))
    monkeypatch.setattr("app.services.notifier.send_telegram", _finto)
    return inviati


@pytest.mark.asyncio
async def test_supervisore_segnala_il_job_congelato(db_session, _enqueue_scartato,
                                                    _telegram_spia, fake_redis):
    """Score gia' scaduto in `arq:queue` + chiave in-progress presente = il job
    non partira' mai da solo. Il supervisore non puo' riaccodarlo (ARQ scarta il
    duplicato) e non deve cancellare la chiave da un altro processo: deve
    urlare."""
    import time

    from app.workers import cron_worker
    from app.workers.wa_worker import wa_send_job_id

    ctx_db = await _scenario_eleggibile(db_session)
    job_id = wa_send_job_id(ctx_db["numero"].id)
    await fake_redis.zadd("arq:queue", {job_id: time.time() * 1000 - 60_000})
    await fake_redis.set(f"arq:in-progress:{job_id}", b"1")

    esito = await cron_worker.wa_campaign_supervisor({"redis": fake_redis})

    assert esito["congelate"] == 1
    assert esito["riaccodate"] == 0
    assert len(_telegram_spia) == 1
    messaggio, livello = _telegram_spia[0]
    assert livello == "error"
    assert job_id in messaggio
    # La chiave NON si tocca da qui: questo processo non e' quello che esegue
    # il job e non sa se il worker principale lo stia davvero lavorando.
    assert await fake_redis.exists(f"arq:in-progress:{job_id}")


@pytest.mark.asyncio
async def test_supervisore_muto_mentre_la_mini_sessione_lavora(db_session, _enqueue_scartato,
                                                               _telegram_spia, fake_redis):
    """ARQ toglie il job dal sorted set solo in `finish_job`: per TUTTA la
    durata di una mini-sessione normale (8-17 minuti) lo score in coda resta
    quello vecchio e la chiave in-progress c'e'. A Redis, alla lettera, e'
    identico a un congelamento -- e il supervisore gira ogni 15 minuti, quindi
    senza questo controllo urlerebbe a ogni giro mentre tutto va bene, cioe'
    insegnerebbe a ignorare l'allarme prima del congelamento vero.

    Cio' che distingue i due casi e' il lucchetto di profilo, che
    esegui_mini_sessione tiene per tutta la sessione e rinnova a ogni
    messaggio."""
    import time

    from app.services import wa_profile_lock
    from app.workers import cron_worker
    from app.workers.wa_worker import wa_send_job_id

    ctx_db = await _scenario_eleggibile(db_session)
    number_id = ctx_db["numero"].id
    job_id = wa_send_job_id(number_id)
    await fake_redis.zadd("arq:queue", {job_id: time.time() * 1000 - 60_000})
    await fake_redis.set(f"arq:in-progress:{job_id}", b"1")
    await fake_redis.set(wa_profile_lock.lock_key(number_id),
                         f"token-vivo:{int(time.time() * 1000)}")

    esito = await cron_worker.wa_campaign_supervisor({"redis": fake_redis})

    assert esito["congelate"] == 0
    assert _telegram_spia == []


@pytest.mark.asyncio
async def test_supervisore_muto_sul_break_fra_mini_sessioni(db_session, _enqueue_scartato,
                                                            _telegram_spia, fake_redis):
    """Controprova: job schedulato nel FUTURO e nessuna chiave in-progress. E' il
    break anti-ban fra due mini-sessioni, cioe' il caso piu' frequente in cui
    l'enqueue viene scartato: se facesse rumore qui, l'allarme diventerebbe
    inutile in un giorno."""
    import time

    from app.workers import cron_worker
    from app.workers.wa_worker import wa_send_job_id

    ctx_db = await _scenario_eleggibile(db_session)
    job_id = wa_send_job_id(ctx_db["numero"].id)
    await fake_redis.zadd("arq:queue", {job_id: time.time() * 1000 + 15 * 60_000})

    esito = await cron_worker.wa_campaign_supervisor({"redis": fake_redis})

    assert esito["congelate"] == 0
    assert _telegram_spia == []


@pytest.mark.asyncio
async def test_il_secondo_giro_conta_il_congelamento_ma_non_riscrive_su_telegram(
        db_session, _enqueue_scartato, _telegram_spia, fake_redis):
    """La chiave in-progress dura SEI ore e il supervisore gira ogni 15 minuti:
    senza freno un solo incidente varrebbe fino a 24 messaggi, e un allarme che
    martella e' un allarme che si impara a ignorare. Il congelamento deve
    restare contato -- il canale e' ancora fermo -- ma Telegram tace."""
    import time

    from app.workers import cron_worker
    from app.workers.wa_worker import wa_send_job_id

    ctx_db = await _scenario_eleggibile(db_session)
    job_id = wa_send_job_id(ctx_db["numero"].id)
    await fake_redis.zadd("arq:queue", {job_id: time.time() * 1000 - 60_000})
    await fake_redis.set(f"arq:in-progress:{job_id}", b"1")

    primo = await cron_worker.wa_campaign_supervisor({"redis": fake_redis})
    secondo = await cron_worker.wa_campaign_supervisor({"redis": fake_redis})

    assert primo["congelate"] == 1 and secondo["congelate"] == 1, (
        "il secondo giro deve continuare a CONTARE il congelamento: il canale "
        "e' ancora fermo, e' solo la notifica che si silenzia")
    assert len(_telegram_spia) == 1, (
        f"{len(_telegram_spia)} messaggi Telegram invece di 1: il cooldown "
        "non sta trattenendo il secondo allarme")


@pytest.mark.asyncio
async def test_l_allarme_non_suggerisce_di_cancellare_la_chiave_alla_cieca(
        db_session, _enqueue_scartato, _telegram_spia, fake_redis):
    """Il testo dell'alert istruisce un umano, che agisce minuti dopo il tick:
    nel frattempo una sessione nuova puo' essere partita, e cancellare la
    chiave di un job VIVO lo fa ripescare. Il messaggio deve nominare il
    lucchetto da controllare prima, non solo il comando DEL."""
    import time

    from app.workers import cron_worker
    from app.workers.wa_worker import wa_send_job_id

    ctx_db = await _scenario_eleggibile(db_session)
    number_id = ctx_db["numero"].id
    job_id = wa_send_job_id(number_id)
    await fake_redis.zadd("arq:queue", {job_id: time.time() * 1000 - 60_000})
    await fake_redis.set(f"arq:in-progress:{job_id}", b"1")

    await cron_worker.wa_campaign_supervisor({"redis": fake_redis})

    messaggio = _telegram_spia[0][0]
    assert f"wa:profile-lock:{number_id}" in messaggio, (
        "l'alert propone un DEL manuale senza dire di verificare prima il "
        "lucchetto di profilo: e' l'azione che il codice stesso si vieta")


@pytest.mark.asyncio
async def test_se_il_cooldown_non_e_verificabile_l_allarme_parte_lo_stesso(
        db_session, _enqueue_scartato, _telegram_spia, fake_redis, monkeypatch):
    """Il freno al rumore non deve poter spegnere l'allarme che frena. Se il
    SET del cooldown solleva, l'eccezione risalirebbe al try per-campagna del
    supervisore e il congelamento non verrebbe ne' contato ne' segnalato: un
    blip di Redis renderebbe muto proprio il guasto da vedere."""
    import time

    from app.workers import cron_worker
    from app.workers.wa_worker import wa_send_job_id

    ctx_db = await _scenario_eleggibile(db_session)
    job_id = wa_send_job_id(ctx_db["numero"].id)
    await fake_redis.zadd("arq:queue", {job_id: time.time() * 1000 - 60_000})
    await fake_redis.set(f"arq:in-progress:{job_id}", b"1")

    async def _set_rotto(*a, **kw):
        raise ConnectionError("redis irraggiungibile")
    monkeypatch.setattr(fake_redis, "set", _set_rotto)

    esito = await cron_worker.wa_campaign_supervisor({"redis": fake_redis})

    assert esito["congelate"] == 1, (
        "un cooldown non verificabile ha inghiottito il congelamento")
    assert len(_telegram_spia) == 1, (
        "nel dubbio si urla: l'allarme deve partire lo stesso")


# ---------------------------------------------------------------------------
# Ancoraggi: i tre nomi da cui dipende tutto il meccanismo
#
# I due test di pulizia qui sopra scrivono le chiavi Redis come STRINGHE
# LETTERALI. E' voluto -- il loro valore e' l'asserzione negativa, che le
# chiavi del cron e di Instagram restino intatte -- ma significa che restano
# verdi anche se il nome vero cambia, e il fix diventerebbe un no-op silenzioso
# in produzione. Questi tre test legano le costanti alle loro sorgenti, cosi'
# un cambio di nome diventa rosso qui invece che invisibile.
# ---------------------------------------------------------------------------

def test_lo_scan_copre_la_chiave_reale_di_ogni_canale():
    """Se un costruttore di job_id cambiasse formato, o lo scan si restringesse,
    la pulizia smetterebbe di trovare quel canale senza dirlo."""
    import fnmatch

    from app.services.arq_job_recovery import (IN_PROGRESS_PREFIX,
                                               ORPHAN_SCAN_PATTERN)
    from app.services.browser_bio import browser_bio_job_id
    from app.services.browser_import import browser_import_job_id
    from app.services.work_enqueue import dm_worker_job_id
    from app.workers.wa_discover_worker import wa_discover_job_id
    from app.workers.wa_worker import wa_send_job_id

    uuid = "11111111-2222-3333-4444-555555555555"
    for job_id in (wa_send_job_id(uuid), wa_discover_job_id(uuid),
                   browser_bio_job_id(uuid, uuid), browser_import_job_id(uuid, uuid),
                   dm_worker_job_id(uuid, uuid)):
        chiave = IN_PROGRESS_PREFIX + job_id
        assert fnmatch.fnmatch(chiave, ORPHAN_SCAN_PATTERN), (
            f"lo scan {ORPHAN_SCAN_PATTERN!r} non copre la chiave reale "
            f"{chiave!r}: quel canale resterebbe congelato in silenzio")


def test_il_prefisso_in_progress_e_quello_che_usa_arq():
    """L'unica costante che non viene da codice nostro. Se arq lo cambiasse in
    un aggiornamento, tutta la suite resterebbe verde e il fix sarebbe morto."""
    from arq.constants import in_progress_key_prefix

    from app.services.arq_job_recovery import IN_PROGRESS_PREFIX

    assert IN_PROGRESS_PREFIX == in_progress_key_prefix, (
        "arq ha cambiato il prefisso delle chiavi in-progress: la pulizia sta "
        "cercando chiavi che non esistono piu'")


def test_il_prefisso_dei_cron_e_quello_che_usa_arq():
    """La denylist vale solo se `cron:` e' davvero il prefisso che arq mette ai
    job dello scheduler (arq/cron.py: `name = name or 'cron:' + coroutine`). Se
    cambiasse, la denylist diventerebbe muta invece che rossa -- e resterebbe in
    piedi solo il controllo sulla coda, che e' esattamente la ridondanza che
    questa costante serve a garantire."""
    from arq.cron import cron

    from app.services.arq_job_recovery import CRON_JOB_PREFIX

    async def _finta_funzione_cron(ctx): ...

    assert cron(_finta_funzione_cron, minute=0).name.startswith(CRON_JOB_PREFIX), (
        "arq non nomina piu' i cron job con questo prefisso: la denylist non "
        "protegge piu' nulla")


def test_la_coda_letta_e_quella_del_worker_principale_e_non_quella_del_cron():
    """Tutto il perimetro della pulizia poggia su un fatto solo: i job del cron
    worker NON stanno nella coda che questo worker consuma. Se le due code
    diventassero la stessa, il controllo sullo score smetterebbe di discriminare
    e resterebbe in piedi solo la denylist -- va saputo, non scoperto in
    produzione."""
    from app.services.work_enqueue import ARQ_CRON_QUEUE, ARQ_MAIN_QUEUE
    from app.workers import cron_worker, task_queue

    assert task_queue.WorkerSettings.queue_name == ARQ_MAIN_QUEUE, (
        "il worker principale non consuma piu' ARQ_MAIN_QUEUE: la pulizia "
        "guarda la coda sbagliata e non trova i job congelati")
    assert cron_worker.CronWorkerSettings.queue_name == ARQ_CRON_QUEUE, (
        "il cron worker e' finito sulla coda principale: le sue chiavi ora "
        "sono protette solo dalla denylist su `cron:`")
    assert ARQ_MAIN_QUEUE != ARQ_CRON_QUEUE


def test_la_pulizia_e_cablata_nello_startup_del_worker():
    """Il test che monkeypatcha on_startup prova che la chiamata c'e' DENTRO la
    funzione, non che la funzione sia registrata: togliendo `on_startup` dalle
    WorkerSettings resterebbe tutto verde e la pulizia non girerebbe mai."""
    from app.workers import task_queue

    assert task_queue.WorkerSettings.on_startup is task_queue.on_startup, (
        "on_startup non e' piu' registrata nelle WorkerSettings: la pulizia "
        "delle chiavi orfane non gira all'avvio del worker di produzione")
