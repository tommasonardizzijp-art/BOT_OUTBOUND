import asyncio
import uuid

import pytest
import pytest_asyncio
import fakeredis.aioredis

from app.services import wa_profile_lock


@pytest.fixture
def fake_redis(monkeypatch):
    client = fakeredis.aioredis.FakeRedis()

    async def _fake_pool():
        return client

    monkeypatch.setattr(wa_profile_lock.arq, "create_pool", lambda *_a, **_k: _fake_pool())
    return client


@pytest.mark.asyncio
async def test_held_acquisisce_e_rilascia(fake_redis):
    async with wa_profile_lock.held("num-1"):
        assert await fake_redis.exists("wa:profile-lock:num-1")
    assert not await fake_redis.exists("wa:profile-lock:num-1")


@pytest.mark.asyncio
async def test_held_solleva_se_gia_occupato(fake_redis):
    async with wa_profile_lock.held("num-1"):
        with pytest.raises(wa_profile_lock.WaProfileBusy):
            async with wa_profile_lock.held("num-1"):
                pass


@pytest.mark.asyncio
async def test_held_non_rilascia_lock_altrui_scaduto(fake_redis):
    """Se il TTL e' scaduto e un altro possessore ha gia' preso il lock,
    l'uscita del primo `held` NON deve cancellare il lock del secondo --
    e' il motivo per cui si confronta un token, non un DELETE incondizionato."""
    await fake_redis.set("wa:profile-lock:num-1", "token-vecchio", ex=1)
    await asyncio.sleep(1.1)  # Attendi che il TTL scada
    async with wa_profile_lock.held("num-1") as token_nuovo:
        assert token_nuovo != "token-vecchio"
        current = await fake_redis.get("wa:profile-lock:num-1")
        assert wa_profile_lock._token_di(current) == token_nuovo
    assert not await fake_redis.exists("wa:profile-lock:num-1")


@pytest.mark.asyncio
async def test_renew_rimette_il_ttl_pieno(fake_redis, monkeypatch):
    """L'heartbeat deve spostare la scadenza in avanti: senza, una sessione
    piu' lunga del TTL lascerebbe il profilo aperto con il lock gia' libero."""
    from app.config import settings
    monkeypatch.setattr(settings, "wa_profile_lock_ttl_min", 90)

    async with wa_profile_lock.held("num-1", ttl_min=1) as token:
        assert await fake_redis.ttl("wa:profile-lock:num-1") <= 60
        assert await wa_profile_lock.renew("num-1", token) is True
        assert await fake_redis.ttl("wa:profile-lock:num-1") > 60


@pytest.mark.asyncio
async def test_renew_non_tocca_un_lock_di_altri(fake_redis):
    """Stesso principio del rilascio: se il TTL e' scaduto e un altro ha gia'
    preso il lock, il rinnovo del primo non deve prolungare il lock del
    secondo -- prolungherebbe una sessione altrui a sua insaputa."""
    await fake_redis.set("wa:profile-lock:num-1", "token-di-un-altro", ex=600)
    assert await wa_profile_lock.renew("num-1", "il-mio-token-vecchio") is False
    assert (await fake_redis.get("wa:profile-lock:num-1")).decode() == "token-di-un-altro"


@pytest.mark.asyncio
async def test_renew_non_solleva_se_redis_non_risponde(monkeypatch):
    """Un blip Redis non deve abbattere una mini-sessione di invio in corso:
    restano il TTL gia' impostato e il cap wall-clock del chiamante."""
    async def _pool_rotto(*_a, **_k):
        raise ConnectionError("redis irraggiungibile")
    monkeypatch.setattr(wa_profile_lock.arq, "create_pool", _pool_rotto)

    assert await wa_profile_lock.renew("num-1", "token") is False


@pytest.mark.asyncio
async def test_release_stale_rilascia_lock_senza_heartbeat_recente(fake_redis):
    """Simula un worker morto a meta' sessione: heartbeat vecchio, nessun
    renew arrivato. Il cron deve liberarlo senza aspettare i 90 min del TTL."""
    vecchio = f"token-morto:{int((__import__('time').time() - 30 * 60) * 1000)}"
    await fake_redis.set("wa:profile-lock:num-1", vecchio, ex=90 * 60)
    rilasciati = await wa_profile_lock.release_stale(stale_after_min=25)
    assert rilasciati == 1
    assert not await fake_redis.exists("wa:profile-lock:num-1")


@pytest.mark.asyncio
async def test_release_stale_non_tocca_lock_vivo(fake_redis):
    """Un lock con heartbeat fresco (sessione in corso, renew regolari) non
    va toccato: cancellarlo aprirebbe un secondo Chromium sullo stesso
    profilo, il danno esatto che il lock previene."""
    async with wa_profile_lock.held("num-1"):
        rilasciati = await wa_profile_lock.release_stale(stale_after_min=25)
        assert rilasciati == 0
        assert await fake_redis.exists("wa:profile-lock:num-1")


@pytest.mark.asyncio
async def test_release_stale_ignora_valore_senza_heartbeat(fake_redis):
    """Un valore nel vecchio formato (solo token, da prima di questa
    modifica) non ha heartbeat da leggere: fail-safe verso il lock, non
    lo cancella per non rischiare di liberare una sessione viva."""
    await fake_redis.set("wa:profile-lock:num-1", "solo-token-senza-heartbeat", ex=90 * 60)
    rilasciati = await wa_profile_lock.release_stale(stale_after_min=25)
    assert rilasciati == 0
    assert await fake_redis.exists("wa:profile-lock:num-1")


@pytest_asyncio.fixture
async def _redis_o_skip():
    import arq
    from app.services.work_enqueue import arq_redis_settings
    try:
        pool = await arq.create_pool(arq_redis_settings())
        await pool.ping()
        await pool.aclose()
    except Exception:
        pytest.skip("Redis non raggiungibile in questo ambiente")


@pytest.mark.asyncio
async def test_held_vero_contro_redis_reale(_redis_o_skip):
    """Senza monkeypatch: verifica la mutua esclusione vera, non solo la
    logica mockata sopra."""
    from app.services import wa_profile_lock
    number_id = f"lock-test-{uuid.uuid4().hex[:8]}"
    async with wa_profile_lock.held(number_id):
        with pytest.raises(wa_profile_lock.WaProfileBusy):
            async with wa_profile_lock.held(number_id):
                pass
    # rilasciato: una seconda acquisizione ora riesce
    async with wa_profile_lock.held(number_id):
        pass
