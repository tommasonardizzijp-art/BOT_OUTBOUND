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
(dettaglio e prove in app/services/wa_job_recovery.py).

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

@pytest.mark.asyncio
async def test_pulizia_cancella_solo_i_lock_di_invio_wa(fake_redis):
    """Il test che protegge dal danno peggiore. Il cron worker gira in un
    processo SEPARATO, che non si riavvia insieme a questo: cancellargli le sue
    `arq:in-progress:cron:*` significherebbe far rieseguire un cron mentre e' in
    corso. Deve sparire solo `wa:send:*`, e deve restare la chiave `arq:job:`
    (senza quella il job non esiste piu' e non lo raccoglie nessuno)."""
    from app.services.wa_job_recovery import clear_orphan_wa_send_locks

    await fake_redis.set("arq:in-progress:wa:send:num-1", b"1")
    await fake_redis.set("arq:in-progress:wa:send:num-2", b"1")
    intatte = ("arq:in-progress:cron:wa_campaign_supervisor:456",
               "arq:in-progress:worker:camp-1:acc-1",
               "arq:job:wa:send:num-1",
               "wa:profile-lock:num-1")
    for chiave in intatte:
        await fake_redis.set(chiave, b"1")

    ripuliti = await clear_orphan_wa_send_locks(fake_redis)

    assert sorted(ripuliti) == ["wa:send:num-1", "wa:send:num-2"]
    assert not await fake_redis.exists("arq:in-progress:wa:send:num-1")
    assert not await fake_redis.exists("arq:in-progress:wa:send:num-2")
    for chiave in intatte:
        assert await fake_redis.exists(chiave), f"{chiave} non doveva essere toccata"


@pytest.mark.asyncio
async def test_pulizia_senza_chiavi_orfane_non_tocca_nulla(fake_redis):
    """Il caso NORMALE (avvio con Redis pulito) non deve ne' cancellare niente
    ne' sollevare: la pulizia gira a ogni startup del worker di produzione."""
    from app.services.wa_job_recovery import clear_orphan_wa_send_locks

    await fake_redis.set("arq:in-progress:cron:daily_reset:1", b"1")

    ripuliti = await clear_orphan_wa_send_locks(fake_redis)

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
    monkeypatch.setattr(task_queue, "clear_orphan_wa_send_locks", _fake_pulizia)

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

def test_il_glob_della_pulizia_copre_il_job_id_vero_dell_invio():
    """Se `wa_send_job_id` cambiasse formato, ORPHAN_WA_SEND_PATTERN resterebbe
    indietro e la pulizia non troverebbe piu' nulla, in silenzio."""
    import fnmatch

    from app.services.wa_job_recovery import (IN_PROGRESS_PREFIX,
                                              ORPHAN_WA_SEND_PATTERN)
    from app.workers.wa_worker import wa_send_job_id

    chiave = IN_PROGRESS_PREFIX + wa_send_job_id("11111111-2222-3333-4444-555555555555")
    assert fnmatch.fnmatch(chiave, ORPHAN_WA_SEND_PATTERN), (
        f"il glob {ORPHAN_WA_SEND_PATTERN!r} non copre piu' la chiave reale "
        f"{chiave!r}: la pulizia non troverebbe nulla e non lo direbbe")


def test_il_prefisso_in_progress_e_quello_che_usa_arq():
    """L'unica costante che non viene da codice nostro. Se arq lo cambiasse in
    un aggiornamento, tutta la suite resterebbe verde e il fix sarebbe morto."""
    from arq.constants import in_progress_key_prefix

    from app.services.wa_job_recovery import IN_PROGRESS_PREFIX

    assert IN_PROGRESS_PREFIX == in_progress_key_prefix, (
        "arq ha cambiato il prefisso delle chiavi in-progress: la pulizia sta "
        "cercando chiavi che non esistono piu'")


def test_la_pulizia_e_cablata_nello_startup_del_worker():
    """Il test che monkeypatcha on_startup prova che la chiamata c'e' DENTRO la
    funzione, non che la funzione sia registrata: togliendo `on_startup` dalle
    WorkerSettings resterebbe tutto verde e la pulizia non girerebbe mai."""
    from app.workers import task_queue

    assert task_queue.WorkerSettings.on_startup is task_queue.on_startup, (
        "on_startup non e' piu' registrata nelle WorkerSettings: la pulizia "
        "delle chiavi orfane non gira all'avvio del worker di produzione")
