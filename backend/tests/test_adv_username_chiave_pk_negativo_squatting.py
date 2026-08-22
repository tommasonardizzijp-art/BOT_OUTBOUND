"""ADVERSARIALE — `decidi_sostituzione_targa` (app/services/browser_bio.py:479,
usato anche da dm_harvest.py) non verifica che il "pk vero" arrivato nel
payload sia POSITIVO prima di ancorarlo come targa reale.

Il modulo app/services/inbox_browser/targa.py e' esplicito sull'invariante che
tutto il sistema si aspetta (docstring, righe 12-13): "NEGATIVA per
costruzione: Instagram non assegna pk negativi, quindi la collisione con una
targa reale e' impossibile, non improbabile." L'invariante regge SOLO se ogni
punto che scrive un "pk vero" in `ig_user_id` verifica che sia positivo.
`decidi_sostituzione_targa` non lo fa: `int(pk_vero)` accetta qualunque intero,
compreso uno negativo, e ritorna 'sostituisci' esattamente come per un pk
legittimo.

Conseguenza dimostrata: se un payload arriva con un pk negativo (dato
corrotto/malformato lato captured GraphQL — non e' richiesto un attacco attivo,
basta un payload sporco), `harvest_profile_into_follower` scrive quel valore
negativo in `ig_user_id` come se fosse "risolto". Se quel valore negativo
combacia per caso con la targa provvisoria che un ALTRO contatto della stessa
campagna calcolera' piu' tardi (SHA256 dello username, funzione pura e
pubblica — chiunque puo' calcolarla), quel secondo contatto non entrera' MAI
in tabella: l'INSERT collide sull'UniqueConstraint(campaign_id, ig_user_id) e
viene scartato in silenzio da run_inbox_list (`except IntegrityError: ...
scartati += 1`). Un contatto vero sparisce per sempre, senza errore visibile.
"""
import asyncio
import os
import tempfile

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models.account  # noqa: F401
import app.models.activity_log  # noqa: F401
import app.models.campaign  # noqa: F401
import app.models.campaign_account  # noqa: F401
import app.models.global_contact  # noqa: F401
import app.models.imported_profile  # noqa: F401
import app.models.message  # noqa: F401
import app.models.user  # noqa: F401

from app.database import Base
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.services.browser_bio import decidi_sostituzione_targa
from app.services.dm_harvest import harvest_profile_into_follower
from app.services.inbox_browser.targa import targa_provvisoria, e_provvisoria


def test_decidi_sostituzione_targa_non_verifica_il_segno_del_pk_vero():
    """La funzione pura: un pk_vero negativo viene trattato come un pk reale
    legittimo (stesso esito di uno positivo), invece di essere rifiutato come
    dato impossibile secondo l'invariante di targa.py."""
    provvisoria = targa_provvisoria("vittima_ignara")
    esito = decidi_sostituzione_targa(provvisoria, -5)
    assert esito != "sostituisci", (
        f"decidi_sostituzione_targa ha accettato un pk_vero negativo (-5) come "
        f"se fosse un pk Instagram reale (esito={esito!r}). Instagram non "
        "assegna mai pk negativi: un negativo qui e' un dato corrotto, non un "
        "contatto risolto."
    )


@pytest_asyncio.fixture
async def _db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="adv_pk_negativo_")
    os.close(fd)
    url = f"sqlite+aiosqlite:///{path}"
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield session_factory
    await engine.dispose()
    try:
        os.remove(path)
    except OSError:
        pass


@pytest.mark.asyncio
async def test_harvest_con_pk_negativo_squatta_la_targa_di_un_altro_contatto(_db):
    session_factory = _db

    async with session_factory() as db:
        campagna = Campaign(
            name="adv-pk-negativo", target_username="t", source_type="scrape",
            status=CampaignStatus.draft,
        )
        db.add(campagna)
        await db.commit()
        cid = campagna.id

        # Contatto A: ha gia' una targa provvisoria (canale browser, prima di
        # questo cantiere). Riceve un DM, l'harvest passivo scatta col payload
        # captured — e quel payload porta un pk NEGATIVO (dato corrotto).
        contatto_a = Follower(
            campaign_id=cid, ig_user_id=targa_provvisoria("contatto_a"),
            username="contatto_a", status=FollowerStatus.sent,
        )
        db.add(contatto_a)
        await db.commit()

        # Il pk "vero" nel payload e' esattamente la targa provvisoria che un
        # contatto B (non ancora scoperto) calcolera' da solo piu' tardi.
        pk_squattato = targa_provvisoria("contatto_b_non_ancora_scoperto")
        payload = {"id": str(pk_squattato), "username": "contatto_a",
                   "biography": "bio catturata passivamente"}

        targa_iniziale = contatto_a.ig_user_id
        await harvest_profile_into_follower(db, contatto_a, payload)

        # LA DIFESA: il pk non positivo non viene trattato come targa reale.
        # Prima della correzione del 22/08 l'harvest lo scriveva, e il seguito di
        # questo test dimostrava il danno; ora la riga resta com'era.
        assert contatto_a.ig_user_id == targa_iniziale, (
            "un pk non positivo e' stato scritto come targa reale: "
            "decidi_sostituzione_targa non sta piu' verificando il segno"
        )

        # E il contatto B, quando la discesa lo scopre davvero, entra senza
        # collidere. Era questo il danno: senza la difesa il suo INSERT
        # sollevava sull'UNIQUE e run_inbox_list lo scartava in silenzio
        # ("except IntegrityError: ... scartati += 1") — un contatto vero
        # spariva per sempre senza un errore visibile.
        contatto_b = Follower(
            campaign_id=cid, ig_user_id=pk_squattato,
            username="contatto_b_non_ancora_scoperto", status=FollowerStatus.pending,
        )
        db.add(contatto_b)
        await db.commit()
        assert contatto_b.ig_user_id == pk_squattato
