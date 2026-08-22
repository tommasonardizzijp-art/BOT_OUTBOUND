"""Riapertura mirata della discesa inbox, via API reale.

`inbox_bottom_reached` e' un interruttore PERMANENTE: una volta alzato, la
campagna guarda solo la cima della lista e non scende mai piu'. Se quel "sono in
fondo" era una bugia di Instagram (tetto di profondita', payload degradato,
blip), l'unica uscita era il reset generale della campagna — che pero' cancella
tutti i Message e riporta indietro lo stato di tutti i follower.

Questi test provano che la riapertura mirata fa SOLO il suo mestiere: rimette in
discesa e non tocca nient'altro.
"""
import asyncio
import os
import tempfile
import uuid
from datetime import datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models.account            # noqa: F401 — registra le tabelle su Base
import app.models.activity_log       # noqa: F401
import app.models.campaign_account   # noqa: F401
import app.models.follower           # noqa: F401
import app.models.global_contact     # noqa: F401
import app.models.imported_profile   # noqa: F401
import app.models.message            # noqa: F401
import app.models.user               # noqa: F401

from app.database import Base, get_db
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.models.message import Message
from app.models.user import User
from app.utils.auth_deps import get_current_user


@pytest.fixture(scope="module")
def _temp_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="riapri_discesa_")
    os.close(fd)
    engine = create_async_engine("sqlite+aiosqlite:///{}".format(path),
                                 connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    yield engine, factory
    asyncio.run(engine.dispose())
    try:
        os.remove(path)
    except OSError:
        pass


@pytest.fixture(scope="module")
def client(_temp_db):
    engine, factory = _temp_db

    async def _override_get_db():
        async with factory() as session:
            try:
                yield session
            finally:
                await session.close()

    def _override_get_current_user():
        return User(
            id="00000000-0000-0000-0000-000000000009",
            email="admin@test.local", password_hash="x", role="admin",
            is_active=True, created_at=datetime.utcnow(),
        )

    from app.main import app
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _override_get_current_user

    from fastapi.testclient import TestClient
    c = TestClient(app, raise_server_exceptions=True)
    yield c
    app.dependency_overrides.clear()


def _run(factory, fn):
    async def _wrap():
        async with factory() as db:
            return await fn(db)
    return asyncio.run(_wrap())


def _crea_campagna_in_fondo(factory, *, scrape_mode="dm_threads"):
    """Campagna inbox che si e' dichiarata in fondo, con un contatto e un messaggio."""
    cid = str(uuid.uuid4())

    async def _seed(db):
        db.add(Campaign(
            id=cid, name="inbox in fondo", source_type="scrape",
            scrape_mode=scrape_mode, inbox_engine="api",
            status=CampaignStatus.ready, messaging_enabled=False,
            inbox_bottom_reached=True,
            inbox_deep_cursor='{"cursor_timestamp_seconds":1772054057}',
            inbox_deep_pages=317,
            created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
        ))
        await db.commit()
        f = Follower(campaign_id=cid, ig_user_id=12345, username="tizio",
                     status=FollowerStatus.sent)
        db.add(f)
        await db.commit()
        from app.models.message import MessageStatus
        db.add(Message(campaign_id=cid, follower_id=f.id,
                       generated_text="ciao", status=MessageStatus.sent))
        await db.commit()

    _run(factory, _seed)
    return cid


def test_riapre_la_discesa_azzerando_solo_i_tre_campi(client, _temp_db):
    engine, factory = _temp_db
    cid = _crea_campagna_in_fondo(factory)

    r = client.post("/api/campaigns/{}/inbox/riapri-discesa".format(cid))
    assert r.status_code == 200, r.text

    async def _leggi(db):
        return await db.get(Campaign, cid)

    c = _run(factory, _leggi)
    assert c.inbox_bottom_reached is False, "la discesa deve essere riaperta"
    assert c.inbox_deep_cursor is None, \
        "il cursore va azzerato: si riparte dalla cima, il dedup rende innocuo il riattraversamento"
    assert c.inbox_deep_pages == 0


def test_NON_cancella_messaggi_contatti_ne_stati(client, _temp_db):
    """La differenza con il reset generale, che invece cancella tutto. E' l'unica
    ragione per cui questo endpoint esiste."""
    engine, factory = _temp_db
    cid = _crea_campagna_in_fondo(factory)

    r = client.post("/api/campaigns/{}/inbox/riapri-discesa".format(cid))
    assert r.status_code == 200, r.text

    async def _conta(db):
        msg = await db.scalar(select(func.count(Message.id))
                              .where(Message.campaign_id == cid))
        fol = (await db.execute(select(Follower)
                                .where(Follower.campaign_id == cid))).scalars().all()
        return msg, fol

    msg, follower = _run(factory, _conta)
    assert msg == 1, "i messaggi non si toccano"
    assert len(follower) == 1, "i contatti non si toccano"
    assert follower[0].status == FollowerStatus.sent, \
        "gli stati dei follower non tornano indietro"


def test_rifiuta_le_campagne_che_non_sono_inbox(client, _temp_db):
    """Su una campagna follower la discesa inbox non esiste: meglio un 400 che
    azzerare in silenzio tre campi che li' non significano nulla."""
    engine, factory = _temp_db
    cid = _crea_campagna_in_fondo(factory, scrape_mode="followers")

    r = client.post("/api/campaigns/{}/inbox/riapri-discesa".format(cid))
    assert r.status_code == 400, r.text


def test_e_idempotente_su_una_campagna_gia_in_discesa(client, _temp_db):
    """Chiamarlo due volte non deve rompere niente: e' una rete di sicurezza, e
    una rete che esplode al secondo strappo non serve."""
    engine, factory = _temp_db
    cid = _crea_campagna_in_fondo(factory)

    assert client.post("/api/campaigns/{}/inbox/riapri-discesa".format(cid)).status_code == 200
    r2 = client.post("/api/campaigns/{}/inbox/riapri-discesa".format(cid))
    assert r2.status_code == 200, r2.text

    async def _leggi(db):
        return await db.get(Campaign, cid)

    c = _run(factory, _leggi)
    assert c.inbox_bottom_reached is False


def test_azzera_ANCHE_scrape_cursor_o_non_riparte_dalla_cima(client, _temp_db):
    """Difetto trovato in review: azzerare solo `inbox_deep_cursor` non basta.
    All'avvio del giro un travaso rimette dentro `scrape_cursor` quando il campo
    profondo e' vuoto, e `scrape_cursor` resta valorizzato ogni volta che il giro
    precedente e' uscito dalla pausa di sessione — cioe' quasi sempre, in una
    discesa lunga. Senza questa riga l'operatore riprende esattamente dal cursore
    di cui, per ipotesi, non si fida piu'. In silenzio."""
    engine, factory = _temp_db
    cid = _crea_campagna_in_fondo(factory)

    async def _metti_scrape_cursor(db):
        c = await db.get(Campaign, cid)
        c.scrape_cursor = '{"cursor_timestamp_seconds":1772054057}'
        await db.commit()

    _run(factory, _metti_scrape_cursor)

    r = client.post("/api/campaigns/{}/inbox/riapri-discesa".format(cid))
    assert r.status_code == 200, r.text

    async def _leggi(db):
        return await db.get(Campaign, cid)

    c = _run(factory, _leggi)
    assert c.inbox_deep_cursor is None
    assert c.scrape_cursor is None, \
        "senza questo il travaso all'avvio rimette dentro il cursore vecchio"


def test_rifiuta_la_riapertura_a_campagna_in_corsa(client, _temp_db):
    """A campagna in `listing` il worker riscrive il cursore a ogni pagina:
    l'azzeramento verrebbe sovrascritto entro pochi secondi e resterebbero solo
    gli altri campi, con la discesa che prosegue da dov'era e il contatore
    ripartito da zero. Un esito che dipende dall'ordine di due scritture non e'
    una rete di sicurezza."""
    engine, factory = _temp_db
    cid = _crea_campagna_in_fondo(factory)

    async def _in_corsa(db):
        c = await db.get(Campaign, cid)
        c.status = CampaignStatus.listing
        await db.commit()

    _run(factory, _in_corsa)

    r = client.post("/api/campaigns/{}/inbox/riapri-discesa".format(cid))
    assert r.status_code == 409, r.text

    c = _run(factory, lambda db: db.get(Campaign, cid))
    assert c.inbox_bottom_reached is True, "rifiutata la chiamata, niente e' cambiato"


def test_il_rilancio_manuale_della_lista_ricarica_il_budget_della_guardia(client, _temp_db,
                                                                          monkeypatch):
    """La rete di sicurezza della discesa si ferma dopo N pagine che non producono
    nulla, e il suo contatore e' PERSISTITO (altrimenti non scatterebbe mai: il
    giro esce ogni 15 pagine). Ma se un avvio manuale non lo ricaricasse, dopo uno
    stop ogni rilancio avanzerebbe di UNA pagina prima di rifar scattare la rete:
    la discesa procederebbe al ritmo di una pagina per intervento, e le sole
    uscite sarebbero buttare via la frontiera o resettare la campagna.

    `inbox_deep_pages` invece NON si ricarica, ed e' giusto: quella e' una misura
    (a che profondita' siamo arrivati), questo e' lo stato di una guardia."""
    engine, factory = _temp_db
    cid = _crea_campagna_in_fondo(factory)

    async def _pronta_al_rilancio(db):
        # L'avvio della lista pretende un account attivo con capability inbox.
        from app.models.account import AccountStatus, InstagramAccount
        from app.models.campaign_account import CampaignAccount
        acct_id = str(uuid.uuid4())
        db.add(InstagramAccount(
            id=acct_id, username="acct_test_{}".format(acct_id[:8]),
            encrypted_password="x", status=AccountStatus.active,
            created_at=datetime.utcnow(),
        ))
        await db.commit()
        db.add(CampaignAccount(campaign_id=cid, account_id=acct_id, role="inbox"))
        await db.commit()

        c = await db.get(Campaign, cid)
        c.status = CampaignStatus.ready
        c.inbox_bottom_reached = False
        c.inbox_deep_senza_lavoro = 2999
        c.inbox_deep_pages = 800
        c.total_followers = 1
        c.list_target = None
        await db.commit()

    _run(factory, _pronta_al_rilancio)

    async def _niente_coda(*a, **k):
        return None

    monkeypatch.setattr("app.services.work_enqueue.enqueue_list", _niente_coda)

    r = client.post("/api/campaigns/{}/list/start".format(cid))
    assert r.status_code == 200, r.text

    c = _run(factory, lambda db: db.get(Campaign, cid))
    assert c.inbox_deep_senza_lavoro == 0, \
        "il budget della guardia va ricaricato da un'azione esplicita dell'operatore"
    assert c.inbox_deep_pages == 800, \
        "la profondita' raggiunta e' una misura, non si azzera: serve negli alert"
