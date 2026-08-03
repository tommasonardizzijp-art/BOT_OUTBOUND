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
        assert current.decode() == token_nuovo
    assert not await fake_redis.exists("wa:profile-lock:num-1")


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
