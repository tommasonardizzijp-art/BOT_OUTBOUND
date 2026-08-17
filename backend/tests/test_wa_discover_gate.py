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
    # Senza questo, ogni test del gate dipenderebbe dalla memoria REALE della
    # macchina che esegue la suite: verde sul PC scarico, rosso a fine giornata
    # con dieci finestre aperte. Un rosso che non parla del codice.
    monkeypatch.setattr(wa_discover_gate, "commit_disponibile_mb", lambda: 20000)


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


@pytest.mark.asyncio
async def test_redis_irraggiungibile_e_fail_closed_non_500(db_session, gate_pulito, monkeypatch):
    # "Tutte fail-closed" (docstring del modulo). Redis giu' e' gia' successo
    # su questa macchina (Memurai ucciso da un taskkill /T, 12/08): senza
    # questo except l'eccezione risale fino all'endpoint e diventa un 500
    # invece di un 409 leggibile.
    async def _esplode(*a, **kw):
        raise ConnectionError("Redis irraggiungibile")

    monkeypatch.setattr(wa_discover_gate.wa_profile_lock, "profilo_occupato_da", _esplode)
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    assert await wa_discover_gate.puo_lanciare(db_session, number) == "browser_occupato"


def test_messaggio_browser_occupato_non_afferma_un_altro_numero():
    # test_browser_occupato_dal_numero_stesso dimostra che lo stesso codice
    # scatta anche quando il lucchetto e' del numero su cui si sta
    # lanciando: il messaggio non puo' affermare che e' "un altro numero",
    # sarebbe falso in quel caso.
    assert "un altro" not in wa_discover_gate.MESSAGGI["browser_occupato"].lower()


@pytest.mark.asyncio
async def test_commit_esaurito_ferma_la_scansione_anche_con_RAM_fisica_abbondante(
        db_session, gate_pulito, monkeypatch):
    """Il caso del 17/08, che il gate non vedeva. Windows concede memoria fino al
    *commit limit* (RAM + pagefile): quando quel tetto si avvicina le richieste
    vengono negate anche se la RAM fisica respira. Quel giorno Memurai ha chiesto
    la riserva per il salvataggio periodico, se l'e' vista rifiutare ed e' rimasto
    appeso -- campagna ferma 90 minuti, e la RAM fisica libera non era il segnale.

    `ram_libera_mb` resta a 4000 di proposito: e' cio' che rende questo test una
    prova. Se il gate guardasse solo la RAM fisica, qui sarebbe verde."""
    monkeypatch.setattr(wa_discover_gate, "commit_disponibile_mb", lambda: 500)
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    assert await wa_discover_gate.puo_lanciare(db_session, number) == "commit_insufficiente"


@pytest.mark.asyncio
async def test_commit_non_misurabile_non_blocca_la_scansione(
        db_session, gate_pulito, monkeypatch):
    """Fuori da Windows `commit_disponibile_mb` ritorna None. Trattare "non lo so"
    come "zero disponibile" spegnerebbe la funzione su ogni Linux -- e' il difetto
    fail-closed-su-sensore-cieco: la guardia piu' severa di cosa sa misurare non
    protegge niente, rifiuta e basta."""
    monkeypatch.setattr(wa_discover_gate, "commit_disponibile_mb", lambda: None)
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    assert await wa_discover_gate.puo_lanciare(db_session, number) is None


def test_commit_disponibile_e_una_misura_diversa_dalla_ram_fisica():
    """Ancoraggio sulla misura vera, non su un doppio. Su Windows il commit
    disponibile deve essere un intero positivo e -- avendo un pagefile -- MAGGIORE
    della sola RAM fisica libera. Se un domani qualcuno "semplificasse"
    `commit_disponibile_mb` facendola tornare a psutil, questo test lo direbbe:
    `psutil.swap_memory().free` misura il pagefile, che e' un'altra cosa (misurati
    insieme il 17/08: commit vero 12044 MB, swap.free 20395 MB)."""
    commit = wa_discover_gate.commit_disponibile_mb()
    if commit is None:
        pytest.skip("non su Windows: il commit non e' misurabile qui")
    assert commit > 0
    assert commit > wa_discover_gate.ram_libera_mb(), (
        "il commit disponibile non supera la RAM fisica libera: la funzione non "
        "sta misurando il commit (RAM + pagefile) ma qualcos'altro")


def test_ogni_codice_di_rifiuto_ha_un_messaggio_per_un_umano():
    # Un 409 senza frase diventa "Errore 409" a schermo, che non dice a
    # nessuno cosa fare dopo.
    for codice in ("numero_non_attivo", "canale_fermo", "browser_occupato",
                   "scan_gia_in_corso", "ram_insufficiente", "commit_insufficiente"):
        assert codice in wa_discover_gate.MESSAGGI
        assert len(wa_discover_gate.MESSAGGI[codice]) > 20
