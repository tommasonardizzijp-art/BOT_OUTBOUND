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
import sqlite3

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


def test_la_regola_non_guarda_piu_la_SORGENTE():
    """La regola e' cambiata di proposito, e questo test la inchioda.

    PRIMA di questo cantiere `valida_ai_senza_bio` prendeva anche `source_type` e
    PERMETTEVA la combinazione su 'import', perche' la passata di risoluzione
    apriva sempre il profilo e salvava la bio a prescindere dal livello. Dopo le
    Task 3-5 quella passata cade: su import il pk arriva dal primo DM, non da una
    visita dedicata, quindi a livello 'none' la bio non arriva piu' nemmeno li'.

    Asserire di nuovo `valida_ai_senza_bio(True, "none") is not None` sarebbe una
    copia dei test qui sopra e non misurerebbe niente (rilievo di review del
    22/08: due test erano esattamente questo). Cio' che si puo' misurare e che
    conta e' che la firma NON torni ad avere un'eccezione per sorgente: se
    qualcuno la reintroducesse, la combinazione vietata si ricreerebbe da li'.
    La copertura per sorgente vive dove la sorgente esiste davvero, cioe' nei
    test HTTP qui sotto (`test_put_import_ai_e_none_ora_e_rifiutato`).
    """
    import inspect

    parametri = list(inspect.signature(valida_ai_senza_bio).parameters)
    assert parametri == ["ai_enabled", "enrichment_level"], (
        f"la guardia ha di nuovo un parametro di troppo: {parametri}. "
        "Se e' una scelta, aggiorna questo test spiegando perche'."
    )


# -- I due verbi HTTP -------------------------------------------------------
# Entrambe le direzioni del PUT, non una: il gate sta a valle dei campi
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


def test_put_accendere_ai_su_campagna_none_e_rifiutato(client):
    r = _crea(client, name="g-patch-ai", ai_enabled=False, enrichment_level="none")
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    p = client.put(f"/api/campaigns/{cid}", json={"ai_enabled": True})
    assert p.status_code == 400, p.text
    assert "Solo DM" in p.json()["detail"]


def test_put_abbassare_livello_su_campagna_ai_e_rifiutato(client):
    r = _crea(client, name="g-patch-liv", ai_enabled=True, enrichment_level="bio")
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    p = client.put(f"/api/campaigns/{cid}", json={"enrichment_level": "none"})
    assert p.status_code == 400, p.text
    assert "Solo DM" in p.json()["detail"]


def test_put_import_ai_e_none_ora_e_rifiutato(client):
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


# -- Una campagna gia' nello stato vietato resta MODIFICABILE ----------------
# Rilievo di review avversariale del 22/08, verificato in produzione: esiste una
# campagna (BORDERLINE X LISTA 7) creata prima di questa guardia, con l'AI accesa e
# il livello 'none' insieme. Valutando la combinazione finale a OGNI put, quella
# campagna rifiutava qualunque modifica — compreso l'abbassamento di daily_limit,
# che e' la leva anti-ban d'emergenza. Una guardia che blocca la manovra di
# sicurezza e' peggio del difetto che previene.

def _campagna_legacy_vietata(client, engine):
    """Riproduce lo stato storico: creata lecita, poi portata a mano nella
    combinazione che oggi la guardia rifiuterebbe in creazione.

    L'UPDATE va fatto sul file sqlite DELLA FIXTURE (`engine.url.database`), non
    su `os.environ["DATABASE_URL"]`: la fixture si crea un DB temporaneo suo con
    mkstemp. La prima versione di questo helper scriveva sul DB sbagliato, quindi
    la campagna restava con l'AI spenta e i test passavano identici con la guardia
    corretta e con quella incondizionata — cioe' non misuravano niente.
    """
    r = _crea(client, name=f"legacy-{id(engine)}", ai_enabled=False, enrichment_level="none")
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    assert client.put(f"/api/campaigns/{cid}", json={"ai_enabled": True}).status_code == 400,         "la guardia deve rifiutare questa transizione dall'API"

    con = sqlite3.connect(engine.url.database)
    con.execute("UPDATE campaigns SET ai_enabled = 1 WHERE id = ?", (cid,))
    con.commit()
    con.close()

    # Precondizione: senza questa, un helper rotto renderebbe i test qui sotto
    # verdi per il motivo sbagliato.
    letta = client.get(f"/api/campaigns/{cid}").json()
    assert letta["ai_enabled"] is True, "lo stato legacy non e' stato riprodotto"
    assert (letta.get("enrichment_level") or "none") == "none"
    return cid


def test_campo_estraneo_resta_modificabile_su_campagna_gia_vietata(client, _temp_db):
    cid = _campagna_legacy_vietata(client, _temp_db[0])
    p = client.put(f"/api/campaigns/{cid}", json={"daily_limit": 12})
    assert p.status_code == 200, p.text
    assert p.json()["daily_limit"] == 12


def test_la_via_di_uscita_resta_aperta(client, _temp_db):
    """Spegnere l'AI, o alzare il livello: sono i due modi di uscire dallo stato
    vietato e devono restare sempre percorribili."""
    cid = _campagna_legacy_vietata(client, _temp_db[0])
    assert client.put(f"/api/campaigns/{cid}", json={"enrichment_level": "bio"}).status_code == 200
    cid2 = _campagna_legacy_vietata(client, _temp_db[0])
    assert client.put(f"/api/campaigns/{cid2}", json={"ai_enabled": False}).status_code == 200


def test_ma_peggiorare_resta_vietato(client):
    """La scappatoia non deve diventare un varco: chi tocca uno dei due campi
    viene comunque valutato sulla combinazione finale."""
    r = _crea(client, name="legacy-peggiora", ai_enabled=False, enrichment_level="bio")
    cid = r.json()["id"]
    assert client.put(f"/api/campaigns/{cid}", json={"ai_enabled": True}).status_code == 200
    p = client.put(f"/api/campaigns/{cid}", json={"enrichment_level": "none"})
    assert p.status_code == 400, p.text


# -- L'AVVIO, non solo la configurazione ------------------------------------
# I due gate HTTP impediscono di CREARE la combinazione vietata, non di USARLA se
# esiste gia': una campagna nata prima della guardia partiva lo stesso. In
# produzione ce n'era una (BORDERLINE X LISTA 7, misurata il 22/08). La guardia
# all'avvio sta in `ensure_campaign_can_send_messages` perche' e' il gate condiviso
# da tutti i percorsi di avvio E dal worker: coprire i quattro endpoint uno per uno
# avrebbe lasciato scoperto proprio il worker, che e' quello che genera i DM.

def _finta_campagna(ai_enabled, enrichment_level, template="Ciao, ti va di sentirci?"):
    from types import SimpleNamespace
    return SimpleNamespace(
        messaging_enabled=True, base_message_template=template,
        ai_enabled=ai_enabled, enrichment_level=enrichment_level,
    )


def test_l_avvio_rifiuta_la_combinazione_vietata():
    from app.services.campaign_control import (
        CampaignControlError, ensure_campaign_can_send_messages,
    )
    with pytest.raises(CampaignControlError) as e:
        ensure_campaign_can_send_messages(_finta_campagna(True, "none"))
    assert "Solo DM" in str(e.value)
    # Deve dire cosa fare, non solo che e' vietato.
    assert "spegni la personalizzazione AI" in str(e.value)


@pytest.mark.parametrize("ai,livello", [
    (True, "bio"), (True, "contacts"), (False, "none"), (False, "bio"),
])
def test_l_avvio_non_ostacola_le_combinazioni_sane(ai, livello):
    from app.services.campaign_control import ensure_campaign_can_send_messages
    ensure_campaign_can_send_messages(_finta_campagna(ai, livello))


def test_la_campagna_che_gira_oggi_in_produzione_non_viene_bloccata():
    """PRIMERO ADV3 DM X VDF, 'running' il 22/08: scrape, livello 'bio', AI spenta.
    Misurata prima di introdurre questa guardia proprio per assicurarsi che non la
    fermasse. Se un domani questo test diventa rosso, la guardia si e' allargata."""
    from app.services.campaign_control import ensure_campaign_can_send_messages
    ensure_campaign_can_send_messages(_finta_campagna(False, "bio"))


def test_l_ordine_dei_controlli_non_maschera_i_precedenti():
    """La guardia nuova sta in fondo: chi non ha il template deve continuare a
    sentirsi dire che manca il template, non che l'AI e' incompatibile."""
    from app.services.campaign_control import (
        CampaignControlError, ensure_campaign_can_send_messages,
    )
    with pytest.raises(CampaignControlError) as e:
        ensure_campaign_can_send_messages(_finta_campagna(True, "none", template="ciao"))
    assert "Template" in str(e.value)
