"""La modalita' segnalibro viaggia verso il worker via Redis, non come colonna
DB: vale per una sessione sola. Il worker ARQ carica la campagna da capo
(nuovo processo, nuova query), quindi un attributo runtime impostato dalla
richiesta API non gli arriverebbe mai — serve un canale fuori dal modello ORM.
"""
import fakeredis.aioredis
import pytest

from app.services import work_enqueue


@pytest.fixture
def fake_redis(monkeypatch):
    import redis.asyncio as aioredis

    client = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(aioredis, "from_url", lambda *a, **k: client)
    return client


@pytest.mark.asyncio
async def test_segna_attiva_si_legge_come_attiva(fake_redis):
    await work_enqueue.segna_modalita_segnalibro("camp-1", True)
    assert await work_enqueue.modalita_segnalibro_attiva("camp-1") is True


@pytest.mark.asyncio
async def test_senza_segnare_nulla_e_spenta_di_default():
    """Una campagna mai toccata dalla modalita' segnalibro non ha nulla in
    Redis: deve leggersi come spenta, non come errore."""
    assert await work_enqueue.modalita_segnalibro_attiva("camp-mai-vista") is False


@pytest.mark.asyncio
async def test_segnare_spenta_pulisce_un_flag_precedente(fake_redis):
    """Una sessione precedente aveva acceso la modalita' (TTL 6h): quella dopo
    NON la chiede. Se il flag restasse acceso per il TTL residuo, la sessione
    che non l'ha chiesta salterebbe righe senza saperlo — deve pulirsi ogni
    volta, non solo quando si accende."""
    await work_enqueue.segna_modalita_segnalibro("camp-1", True)
    assert await work_enqueue.modalita_segnalibro_attiva("camp-1") is True

    await work_enqueue.segna_modalita_segnalibro("camp-1", False)
    assert await work_enqueue.modalita_segnalibro_attiva("camp-1") is False


@pytest.mark.asyncio
async def test_campagne_diverse_non_si_influenzano(fake_redis):
    await work_enqueue.segna_modalita_segnalibro("camp-1", True)
    assert await work_enqueue.modalita_segnalibro_attiva("camp-2") is False
