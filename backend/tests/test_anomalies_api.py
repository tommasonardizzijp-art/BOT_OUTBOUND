from datetime import datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.models.user import User
from app.utils.auth_deps import get_current_user


def _admin_utente() -> User:
    return User(id="00000000-0000-0000-0000-0000000000a1", email="admin-anomalies@test.local",
                password_hash="x", role="admin", is_active=True,
                created_at=datetime.utcnow())


def _operatore_utente() -> User:
    return User(id="00000000-0000-0000-0000-0000000000a2", email="op-anomalies@test.local",
                password_hash="x", role="operator", is_active=True,
                created_at=datetime.utcnow())


@pytest_asyncio.fixture
async def client_admin(db_session):
    from app.database import get_db
    app.dependency_overrides[get_current_user] = _admin_utente
    app.dependency_overrides[get_db] = lambda: db_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client_operatore(db_session):
    from app.database import get_db
    app.dependency_overrides[get_current_user] = _operatore_utente
    app.dependency_overrides[get_db] = lambda: db_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("metodo,path", [
    ("GET", "/api/anomalies"),
    ("GET", "/api/anomalies/summary"),
    ("POST", "/api/anomalies/qualunque-id/ack"),
])
async def test_senza_token_e_401(metodo, path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.request(metodo, path)
    assert r.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("metodo,path", [
    ("GET", "/api/anomalies"),
    ("GET", "/api/anomalies/summary"),
    ("POST", "/api/anomalies/qualunque-id/ack"),
])
async def test_utente_non_admin_e_403(metodo, path, client_operatore):
    r = await client_operatore.request(metodo, path)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_admin_lista_e_summary_200(client_admin):
    r = await client_admin.get("/api/anomalies")
    assert r.status_code == 200
    r = await client_admin.get("/api/anomalies/summary")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_admin_ack_su_id_inesistente_e_404_non_403(client_admin):
    r = await client_admin.post("/api/anomalies/non-esiste/ack")
    assert r.status_code == 404
