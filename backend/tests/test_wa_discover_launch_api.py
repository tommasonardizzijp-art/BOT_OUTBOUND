from datetime import datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api import wa_numbers
from app.database import get_db
from app.main import app
from app.models.user import User
from app.models.wa import WaNumberStatus
from app.services import wa_discover_runs
from app.utils.auth_deps import get_current_user
from tests.factories_wa import make_discover_run, make_number, make_tenant


def _admin_utente() -> User:
    return User(id="00000000-0000-0000-0000-00000000000b", email="admin-wa-launch@test.local",
                password_hash="x", role="admin", is_active=True,
                created_at=datetime.utcnow())


@pytest_asyncio.fixture
async def client(db_session):
    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _admin_utente
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def gate_verde(monkeypatch):
    async def _verde(db, number):
        return None

    accodati = []

    async def _enqueue(number_id, run_id):
        accodati.append((number_id, run_id))
        return True

    monkeypatch.setattr(wa_numbers.wa_discover_gate, "puo_lanciare", _verde)
    monkeypatch.setattr(wa_numbers, "enqueue_wa_discover", _enqueue)
    return accodati


@pytest.mark.asyncio
async def test_post_su_numero_inesistente_404(client, gate_verde):
    r = await client.post("/api/wa/numbers/00000000-0000-0000-0000-000000000000/discover")
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_post_apre_la_run_e_accoda(db_session, client, gate_verde):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await db_session.commit()

    r = await client.post(f"/api/wa/numbers/{number.id}/discover")
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["queued"] is True and corpo["run_id"]
    assert gate_verde == [(number.id, corpo["run_id"])]

    attiva = await wa_discover_runs.run_attiva(db_session, number.id)
    assert attiva is not None and attiva.id == corpo["run_id"]
    assert attiva.avviato_da == "manuale"


@pytest.mark.asyncio
@pytest.mark.parametrize("codice", [
    "numero_non_attivo", "canale_fermo", "browser_occupato",
    "scan_gia_in_corso", "ram_insufficiente",
])
async def test_ogni_rifiuto_e_409_con_la_sua_frase(db_session, client, monkeypatch, codice):
    async def _rifiuta(db, number):
        return codice

    accodati = []

    async def _enqueue(number_id, run_id):
        accodati.append(run_id)
        return True

    monkeypatch.setattr(wa_numbers.wa_discover_gate, "puo_lanciare", _rifiuta)
    monkeypatch.setattr(wa_numbers, "enqueue_wa_discover", _enqueue)

    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await db_session.commit()

    r = await client.post(f"/api/wa/numbers/{number.id}/discover")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["codice"] == codice
    assert len(r.json()["detail"]["messaggio"]) > 20
    # Rifiutato significa: nessuna run aperta e nessun job accodato.
    assert accodati == []
    assert await wa_discover_runs.run_attiva(db_session, number.id) is None


@pytest.mark.asyncio
async def test_se_l_accodamento_fallisce_la_run_non_resta_appesa(db_session, client, monkeypatch):
    # Una run 'running' che nessun job chiudera' mai rende il numero non piu'
    # scansionabile (indice unico parziale): va chiusa subito.
    async def _verde(db, number):
        return None

    async def _enqueue_ko(number_id, run_id):
        return False

    monkeypatch.setattr(wa_numbers.wa_discover_gate, "puo_lanciare", _verde)
    monkeypatch.setattr(wa_numbers, "enqueue_wa_discover", _enqueue_ko)

    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await db_session.commit()

    r = await client.post(f"/api/wa/numbers/{number.id}/discover")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["codice"] == "accodamento_fallito"
    assert await wa_discover_runs.run_attiva(db_session, number.id) is None


@pytest.mark.asyncio
async def test_get_senza_nessuna_run(db_session, client):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await db_session.commit()

    r = await client.get(f"/api/wa/numbers/{number.id}/discover")
    assert r.status_code == 200, r.text
    assert r.json() == {"ultima": None, "storico": [], "in_corso": False}


@pytest.mark.asyncio
async def test_get_espone_ultima_storico_e_in_corso(db_session, client):
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    await make_discover_run(db_session, tenant, number, stato="done", salvate=78,
                            dichiarato=900, copertura=9, motivo="fermato_dopo_stallo")
    await make_discover_run(db_session, tenant, number)  # running
    await db_session.commit()

    r = await client.get(f"/api/wa/numbers/{number.id}/discover")
    corpo = r.json()
    assert corpo["in_corso"] is True
    assert corpo["ultima"]["stato"] == "running"
    assert len(corpo["storico"]) == 2
    chiusa = [s for s in corpo["storico"] if s["stato"] == "done"][0]
    assert (chiusa["salvate"], chiusa["dichiarato"], chiusa["copertura"]) == (78, 900, 9)
    assert chiusa["motivo"] == "fermato_dopo_stallo"


@pytest.mark.asyncio
async def test_get_su_numero_inesistente_404(client):
    r = await client.get("/api/wa/numbers/00000000-0000-0000-0000-000000000000/discover")
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_numero_non_attivo_rifiutato_dal_gate_vero(db_session, client, monkeypatch):
    # Senza mock del gate: la guardia sullo stato deve reggere da sola.
    async def _enqueue(number_id, run_id):
        return True

    monkeypatch.setattr(wa_numbers, "enqueue_wa_discover", _enqueue)
    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant, status=WaNumberStatus.retired)
    await db_session.commit()

    r = await client.post(f"/api/wa/numbers/{number.id}/discover")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["codice"] == "numero_non_attivo"
