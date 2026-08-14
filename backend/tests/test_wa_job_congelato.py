"""Il job di invio WA congelato da una chiave in-progress orfana.

Incidente 14/08: campagna running, 48 contatti eleggibili, zero invii per ore,
nessun errore da nessuna parte. ARQ scrive `arq:in-progress:{job_id}` quando
prende in carico un job e la cancella solo in finish_job: un riavvio del worker
a meta' mini-sessione lascia la chiave orfana, e `start_jobs` salta per sempre
un job che ha la sua chiave in-progress (arq/worker.py, la guardia
`if ongoing_exists or not score or score > timestamp_ms(): continue`). Il job
resta in `arq:queue` con lo score gia' scaduto e nessuno lo esegue fino alla
scadenza del TTL: con job_timeout=3600, fino a un'ora di invii persi per ogni
riavvio fatto mentre il worker lavorava.

Due difese, provate qui: la pulizia all'avvio del worker principale (che
rimuove la causa) e l'allarme del supervisore (che rende visibile il
congelamento invece di contare "0 riaccodate" e tacere)."""
from datetime import datetime, timedelta

import fakeredis.aioredis
import pytest

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
    cc.next_action_at = datetime.utcnow() - timedelta(minutes=1)
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
