import pytest

from app.models.wa import WaNumberStatus
from app.services import wa_discover_gate, wa_discover_runs
from tests.factories_wa import make_number, make_tenant


@pytest.fixture
def gate_pulito(monkeypatch):
    """Tutte le condizioni esterne al verde: ogni test rompe la sua e basta."""
    monkeypatch.setattr(wa_discover_gate.bot_state_service, "is_wa_halted",
                        _async_return(False))
    monkeypatch.setattr(wa_discover_gate.wa_profile_lock, "profilo_occupato_da",
                        _async_return(None))
    monkeypatch.setattr(wa_discover_gate, "ram_libera_mb", lambda: 4000)


def _async_return(valore):
    async def _f(*a, **kw):
        return valore
    return _f


@pytest.mark.asyncio
async def test_verde_quando_tutto_e_a_posto(db_session, gate_pulito):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    assert await wa_discover_gate.puo_lanciare(db_session, number) is None


@pytest.mark.asyncio
async def test_numero_non_attivo(db_session, gate_pulito):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant, status=WaNumberStatus.pending_qr)
    assert await wa_discover_gate.puo_lanciare(db_session, number) == "numero_non_attivo"


@pytest.mark.asyncio
async def test_kill_switch_di_canale(db_session, gate_pulito, monkeypatch):
    monkeypatch.setattr(wa_discover_gate.bot_state_service, "is_wa_halted",
                        _async_return(True))
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    assert await wa_discover_gate.puo_lanciare(db_session, number) == "canale_fermo"


@pytest.mark.asyncio
async def test_browser_occupato_da_UN_ALTRO_numero(db_session, gate_pulito, monkeypatch):
    # Il gate e' GLOBALE: i lock sono per-numero e non si escludono fra loro,
    # ma due browser insieme sono 2,4 GB su una macchina che ne ha 7,5.
    monkeypatch.setattr(wa_discover_gate.wa_profile_lock, "profilo_occupato_da",
                        _async_return("un-altro-numero"))
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    assert await wa_discover_gate.puo_lanciare(db_session, number) == "browser_occupato"


@pytest.mark.asyncio
async def test_browser_occupato_dal_numero_stesso(db_session, gate_pulito, monkeypatch):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    monkeypatch.setattr(wa_discover_gate.wa_profile_lock, "profilo_occupato_da",
                        _async_return(number.id))
    assert await wa_discover_gate.puo_lanciare(db_session, number) == "browser_occupato"


@pytest.mark.asyncio
async def test_scan_gia_in_corso(db_session, gate_pulito):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await wa_discover_runs.apri_run(db_session, tenant_id=tenant.id, number_id=number.id)
    await db_session.commit()
    assert await wa_discover_gate.puo_lanciare(db_session, number) == "scan_gia_in_corso"


@pytest.mark.asyncio
async def test_ram_insufficiente(db_session, gate_pulito, monkeypatch):
    monkeypatch.setattr(wa_discover_gate, "ram_libera_mb", lambda: 300)
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    assert await wa_discover_gate.puo_lanciare(db_session, number) == "ram_insufficiente"


def test_ogni_codice_di_rifiuto_ha_un_messaggio_per_un_umano():
    # Un 409 senza frase diventa "Errore 409" a schermo, che non dice a
    # nessuno cosa fare dopo.
    for codice in ("numero_non_attivo", "canale_fermo", "browser_occupato",
                   "scan_gia_in_corso", "ram_insufficiente"):
        assert codice in wa_discover_gate.MESSAGGI
        assert len(wa_discover_gate.MESSAGGI[codice]) > 20
