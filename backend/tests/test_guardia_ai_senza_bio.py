"""Guardia: AI accesa + livello 'none' ("Solo DM"), dove la bio non arriva mai.

Perche' esiste: il livello 'none' non apre mai il profilo (nessuna Fase Bio, nessuna
risoluzione dedicata), quindi con l'AI accesa il follower arriva alla generazione con
`biography=NULL`, `_build_user_prompt` scrive "(bio vuota)" e la regola 10 del system
prompt fa ricopiare il template. Si spende una chiamata AI per riottenere il testo di
partenza.

Una sola condizione, su TUTTE le sorgenti — niente eccezione per `source_type`.
Fino al cantiere "username chiave di prima classe" (22/08/2026) la guardia era
calibrata piu' stretta e permetteva la combinazione su 'import', perche' la
passata di risoluzione salvava comunque la bio a prescindere dal livello
(import_resolver.py:246, browser_import.py:170). Quella passata cade con lo
username come chiave d'identita': su import il pk arriva ora dal primo DM, non
da una visita dedicata, quindi la bio su 'none' non arriva piu' neanche li'. La
regola torna letterale ovunque (vedi
docs/superpowers/plans/2026-08-22-username-chiave-di-prima-classe.md, Task 7).
"""
import pytest

from app.models.campaign import valida_ai_senza_bio


def test_ai_e_none_e_vietata():
    errore = valida_ai_senza_bio(True, "none")
    assert errore is not None
    assert "Solo DM" in errore
    assert "bio" in errore.lower()


@pytest.mark.parametrize("livello", ["bio", "contacts"])
def test_ai_con_arricchimento_e_permessa(livello):
    assert valida_ai_senza_bio(True, livello) is None


def test_senza_ai_e_none_e_permessa():
    # Modalita' template (es. la campagna DM di Primero adv3): nessuna bio serve.
    assert valida_ai_senza_bio(False, "none") is None


def test_import_ai_e_none_e_vietata_dopo_il_cantiere_username():
    """PRIMA di questo cantiere questa combinazione era permessa (la risoluzione
    import apriva sempre il profilo e salvava la bio a prescindere dal livello).
    Dopo le Task 3-5 di username-chiave-di-prima-classe.md quella passata di
    risoluzione cade: su import il pk arriva dal primo DM, non da una visita
    dedicata, quindi a livello 'none' la bio non arriva piu' nemmeno qui. Il
    test e' stato girato di proposito perche' la regola e' cambiata, non perche'
    era rosso."""
    errore = valida_ai_senza_bio(True, "none")
    assert errore is not None


def test_import_senza_ai_e_none_e_permessa():
    assert valida_ai_senza_bio(False, "none") is None


# -- I due verbi HTTP -------------------------------------------------------
# Entrambe le direzioni del PATCH, non una: il gate sta a valle dei campi
# applicati, quindi deve fermare sia "accendo l'AI su una campagna gia' 'none'"
# sia "abbasso il livello su una campagna che ha gia' l'AI". Un controllo su un
# campo alla volta lascerebbe passare la direzione non controllata.

import asyncio
import os
import tempfile
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Register all ORM tables on Base.metadata.
import app.models.account  # noqa: F401
import app.models.activity_log  # noqa: F401
import app.models.campaign_account  # noqa: F401
import app.models.follower  # noqa: F401
import app.models.global_contact  # noqa: F401
import app.models.imported_profile  # noqa: F401
import app.models.message  # noqa: F401
import app.models.user  # noqa: F401

from app.database import Base, get_db
from app.models.user import User
from app.utils.auth_deps import get_current_user


@pytest.fixture(scope="module")
def _temp_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="e2e_guardia_ai_senza_bio_")
    os.close(fd)
    url = f"sqlite+aiosqlite:///{path}"
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    yield engine, session_factory

    async def _dispose():
        await engine.dispose()

    asyncio.run(_dispose())
    try:
        os.remove(path)
    except OSError:
        pass


@pytest.fixture(scope="module")
def client(_temp_db):
    engine, session_factory = _temp_db

    async def _override_get_db():
        async with session_factory() as session:
            try:
                yield session
            finally:
                await session.close()

    def _override_get_current_user():
        return User(
            id="00000000-0000-0000-0000-000000000005",
            email="admin5@test.local",
            password_hash="x",
            role="admin",
            is_active=True,
            created_at=datetime.utcnow(),
        )

    from app.main import app

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _override_get_current_user

    from fastapi.testclient import TestClient

    c = TestClient(app, raise_server_exceptions=True)
    yield c

    app.dependency_overrides.clear()


def _crea(client, **override):
    corpo = {
        "name": "guardia-test",
        "source_type": "scrape",
        "target_username": "un_target",
        "base_message_template": "Ciao, ti va di sentirci?",
        "ai_enabled": False,
        "enrichment_level": "bio",
    }
    corpo.update(override)
    return client.post("/api/campaigns", json=corpo)


def test_create_rifiuta_ai_e_none(client):
    r = _crea(client, name="g-create", ai_enabled=True, enrichment_level="none")
    assert r.status_code == 400, r.text
    assert "Solo DM" in r.json()["detail"]


def test_patch_accendere_ai_su_campagna_none_e_rifiutato(client):
    r = _crea(client, name="g-patch-ai", ai_enabled=False, enrichment_level="none")
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    p = client.put(f"/api/campaigns/{cid}", json={"ai_enabled": True})
    assert p.status_code == 400, p.text
    assert "Solo DM" in p.json()["detail"]


def test_patch_abbassare_livello_su_campagna_ai_e_rifiutato(client):
    r = _crea(client, name="g-patch-liv", ai_enabled=True, enrichment_level="bio")
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    p = client.put(f"/api/campaigns/{cid}", json={"enrichment_level": "none"})
    assert p.status_code == 400, p.text
    assert "Solo DM" in p.json()["detail"]


def test_patch_import_ai_e_none_ora_e_rifiutato(client):
    """Girato rispetto al piano superato: prima permetteva questa combinazione su
    'import' (la risoluzione salvava la bio a prescindere dal livello). Dopo il
    cantiere username-chiave-di-prima-classe la passata di risoluzione cade e la
    regola diventa una sola condizione su tutte le sorgenti — questo va rosso di
    proposito rispetto al comportamento vecchio, non e' un difetto da aggiustare."""
    r = _crea(client, name="g-import", source_type="import", target_username=None,
              ai_enabled=True, enrichment_level="bio")
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    p = client.put(f"/api/campaigns/{cid}", json={"enrichment_level": "none"})
    assert p.status_code == 400, p.text
    assert "Solo DM" in p.json()["detail"]
