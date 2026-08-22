"""La targa provvisoria e' una chiave legittima, non di serie B.

Prima di questo lavoro `targa_ammessa_in_anagrafica` rifiutava le negative, e
`reservation.try_reserve` di conseguenza ritornava False: il contatto finiva
`skipped` con motivo "already_contacted_globally" e non riceveva MAI il DM.
Non era una protezione contro il doppio invio: era un invio che non partiva.
"""
import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.services.global_contact_service import targa_ammessa_in_anagrafica
from app.services.inbox_browser.targa import targa_provvisoria, normalizza_username


def test_targa_provvisoria_e_ammessa():
    assert targa_ammessa_in_anagrafica(targa_provvisoria("borderline_grow")) is True


def test_pk_reale_e_ammesso():
    assert targa_ammessa_in_anagrafica(12345) is True


def test_zero_e_none_non_sono_targhe():
    assert targa_ammessa_in_anagrafica(0) is False
    assert targa_ammessa_in_anagrafica(None) is False


# ── Il ponte: upsert_lead converge le due rappresentazioni su una riga sola ──
#
# Schema fabbricato con create_all (non alembic): qui non serve il vincolo
# fisico UNIQUE (gia' collaudato su schema migrato in
# test_e2e_username_chiave_global_contacts_index.py), serve solo dimostrare
# che upsert_lead CERCA per username_norm e PROMUOVE la riga, che e' logica
# applicativa indipendente dall'indice.

import app.models.account  # noqa: E402,F401
import app.models.activity_log  # noqa: E402,F401
import app.models.campaign_account  # noqa: E402,F401
import app.models.follower  # noqa: E402,F401
import app.models.imported_profile  # noqa: E402,F401
import app.models.message  # noqa: E402,F401
import app.models.user  # noqa: E402,F401

from app.database import Base  # noqa: E402
from app.models.global_contact import GlobalContact  # noqa: E402
from app.services.global_contact_service import upsert_lead  # noqa: E402
from app.utils.contact_extract import ContactData  # noqa: E402


class _CampaignFinta:
    id = "campagna-chiave-doppia"
    name = "chiave doppia"


class _AccountFinto:
    id = "account-chiave-doppia"
    username = "scraper_account"


@pytest.fixture()
def session_factory(tmp_path):
    db_path = tmp_path / "chiave_doppia.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path.as_posix()}", connect_args={"check_same_thread": False},
    )

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    sf = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield sf
    asyncio.run(engine.dispose())


def _run(session_factory, coro_fn):
    async def _wrap():
        async with session_factory() as db:
            return await coro_fn(db)
    return asyncio.run(_wrap())


def test_stessa_persona_dai_due_canali_e_una_riga_sola(session_factory):
    """Il ponte: un contatto salvato prima con la targa provvisoria (canale
    browser) e poi visto dal canale API con il pk reale NON deve produrre due
    righe in anagrafica. E' l'incidente dei 32 doppioni su 34 (memory
    botoutbound-inbox-doppioni-browser-vs-api)."""
    targa = targa_provvisoria("borderline_grow")

    async def _prima(db):
        await upsert_lead(
            db, ig_user_id=targa, username="borderline_grow", full_name=None,
            biography=None, contacts=ContactData(), campaign=_CampaignFinta(), account=_AccountFinto(),
        )

    async def _poi(db):
        await upsert_lead(
            db, ig_user_id=99887766, username="borderline_grow", full_name="Borderline Grow",
            biography="growshop", contacts=ContactData(), campaign=_CampaignFinta(), account=_AccountFinto(),
        )

    _run(session_factory, _prima)
    _run(session_factory, _poi)

    async def _verifica(db):
        righe = (await db.execute(
            select(GlobalContact).where(GlobalContact.username_norm == "borderline_grow")
        )).scalars().all()
        return righe

    righe = _run(session_factory, _verifica)
    assert len(righe) == 1, f"attesa una riga sola, trovate {len(righe)}"
    assert righe[0].ig_user_id == 99887766, (
        f"la riga non e' stata promossa al pk reale: ig_user_id={righe[0].ig_user_id}"
    )


def test_upsert_lead_valorizza_username_norm(session_factory):
    async def _go(db):
        await upsert_lead(
            db, ig_user_id=555111, username="Mario.Rossi", full_name="Mario",
            biography="", contacts=ContactData(), campaign=_CampaignFinta(), account=_AccountFinto(),
        )
        return (await db.execute(
            select(GlobalContact).where(GlobalContact.ig_user_id == 555111)
        )).scalar_one()

    riga = _run(session_factory, _go)
    assert riga.username_norm == normalizza_username("Mario.Rossi") == "mario.rossi"


def test_upsert_lead_segnaposto_non_riceve_username_norm(session_factory):
    """Un segnaposto ("utente instagram") non ha la forma di un handle: niente
    username_norm, altrimenti tutti i segnaposto collasserebbero sulla stessa
    chiave (vedi handle_valido)."""
    async def _go(db):
        await upsert_lead(
            db, ig_user_id=555222, username="utente instagram", full_name=None,
            biography=None, contacts=ContactData(), campaign=_CampaignFinta(), account=_AccountFinto(),
        )
        return (await db.execute(
            select(GlobalContact).where(GlobalContact.ig_user_id == 555222)
        )).scalar_one()

    riga = _run(session_factory, _go)
    assert riga.username_norm is None


# ── La trappola della release: la prenotazione va rilasciata con la targa ───
# con cui e' stata presa, non con quella che il follower ha DOPO l'harvest.
#
# campaign_orchestrator.py:486 prenota con follower.ig_user_id (la targa
# ancora provvisoria). Fra la prenotazione e il rilascio, dm_harvest sostituisce
# follower.ig_user_id col pk vero (Task 3). Se il rilascio a :737 usa
# follower.ig_user_id (rilasciato, mutato), rilascia la targa SBAGLIATA: la
# prenotazione presa con la targa vecchia resta appesa fino al TTL di 30 minuti,
# tenendo il contatto bloccato per le altre campagne.

from app.services import reservation  # noqa: E402
from app.models.contact_reservation import ContactReservation  # noqa: E402


@pytest.fixture()
def reservation_db(tmp_path):
    db_path = tmp_path / "reservation_trap.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path.as_posix()}", connect_args={"check_same_thread": False},
    )

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    sf = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield sf
    asyncio.run(engine.dispose())


def test_release_con_la_targa_sbagliata_lascia_la_prenotazione_appesa(reservation_db):
    """Riproduce il difetto COSI' COM'E' oggi in campaign_orchestrator.py:737:
    rilasciare con la targa nuova (dopo che l'harvest l'ha sostituita) non
    tocca la riga presa con la targa vecchia — resta in contact_reservations.
    Serve a dimostrare il difetto: il fix va nel chiamante (Task 5, punto 4),
    non in reservation.py, che si limita a fare quello che gli viene chiesto."""
    targa_vecchia = targa_provvisoria("contatto_promosso")
    pk_nuovo = 424242

    async def _go(db):
        riservato = await reservation.try_reserve(targa_vecchia, "job1", "campagna1", db)
        assert riservato is True
        # ... l'harvest sostituisce la targa sul follower (non su questa
        # riga di prenotazione, che resta chiavata sulla targa vecchia) ...
        await reservation.release(pk_nuovo, db)  # <- targa SBAGLIATA (difetto)
        rimaste = (await db.execute(select(ContactReservation))).scalars().all()
        return rimaste

    rimaste = _run(reservation_db, _go)
    assert len(rimaste) == 1 and rimaste[0].ig_user_id == targa_vecchia, (
        "la prenotazione presa con la targa vecchia doveva restare appesa "
        "quando il rilascio usa la targa nuova — se questo assert fallisce, "
        "il chiamante e' stato corretto: aggiorna questo test per verificare "
        "il comportamento CORRETTO (rilascio con la targa di prenotazione)"
    )


def test_release_con_la_targa_di_prenotazione_libera_davvero(reservation_db):
    """Il comportamento CORRETTO che campaign_orchestrator deve ottenere:
    rilasciando con la STESSA targa usata per prenotare, la riga sparisce."""
    targa_vecchia = targa_provvisoria("contatto_promosso_bis")

    async def _go(db):
        riservato = await reservation.try_reserve(targa_vecchia, "job1", "campagna1", db)
        assert riservato is True
        await reservation.release(targa_vecchia, db)  # <- targa di prenotazione (corretto)
        rimaste = (await db.execute(select(ContactReservation))).scalars().all()
        return rimaste

    rimaste = _run(reservation_db, _go)
    assert rimaste == []


def test_una_collisione_su_username_norm_non_fa_saltare_l_invio():
    """`upsert_lead` promette di non sollevare mai ("Best-effort; never raises
    fatally") e viene chiamata dal percorso di invio. Da Task 5 scrive
    `username_norm`, che e' sotto UNIQUE: se un'altra riga porta gia' quel valore
    il commit solleva IntegrityError. Deve restare ingoiata — un contatto non
    registrato in anagrafica e' un fastidio, un invio che esplode e' un guasto.
    """
    import asyncio
    from types import SimpleNamespace

    from app.services.global_contact_service import upsert_lead
    from app.utils.contact_extract import ContactData

    class _DbCheEsplodeAlCommit:
        def __init__(self):
            self.rollback_fatto = False
        async def execute(self, _s):
            class _R:
                def scalars(self_inner):
                    class _S:
                        def first(self_s): return None
                    return _S()
            return _R()
        def add(self, _o): pass
        async def commit(self):
            from sqlalchemy.exc import IntegrityError
            raise IntegrityError("INSERT", {}, Exception("UNIQUE ux_global_contacts_username_norm"))
        async def rollback(self):
            self.rollback_fatto = True

    db = _DbCheEsplodeAlCommit()
    asyncio.run(upsert_lead(
        db, ig_user_id=12345, username="borderline_grow", full_name=None,
        biography=None, contacts=ContactData(),
        campaign=SimpleNamespace(id="c1", name="x"), account=SimpleNamespace(username="a"),
    ))
    assert db.rollback_fatto, "il rollback non e' stato fatto dopo l'IntegrityError"
