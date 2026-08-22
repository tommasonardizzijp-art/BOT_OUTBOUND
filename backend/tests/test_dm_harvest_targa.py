"""L'harvest post-invio ancora la targa provvisoria al pk vero.

Perche' esiste: `send_dm` apre gia' il profilo e cattura gia' il payload GraphQL
(instagram_page.py:399-401) — il pk e' li' dentro, gratis, a ogni invio. Prima di
questo lavoro `dm_harvest` salvava bio e conteggi e BUTTAVA il pk (zero occorrenze
di ig_user_id nel modulo): una targa provvisoria restava provvisoria per sempre,
anche dopo dieci DM, e la finestra "handle riassegnato" non si chiudeva mai.

Con l'ancoraggio al primo invio quella finestra dura un solo DM.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.services.dm_harvest import harvest_profile_into_follower
from app.services.inbox_browser.targa import targa_provvisoria


class _FakeDb:
    def __init__(self, altre_righe=None):
        self.committed = False
        self._altre = altre_righe or []
    async def commit(self):
        self.committed = True
    async def rollback(self):
        pass
    async def execute(self, _stmt):
        righe = self._altre
        class _R:
            def scalar_one_or_none(self_inner):
                return righe[0] if righe else None
        return _R()


def _follower(targa, username="borderline_grow"):
    return SimpleNamespace(
        id="f1", campaign_id="c1", username=username, ig_user_id=targa,
        full_name=None, biography=None, follower_count=None, following_count=None,
        external_url=None, is_private=False, is_verified=False,
        status=None, skip_reason=None, locked_by_account_id="acc1", locked_at="x",
        updated_at=None,
    )


def _payload(pk, username="borderline_grow"):
    return {"id": str(pk), "username": username, "biography": "growshop a Savona"}


def test_targa_provvisoria_diventa_quella_vera():
    f = _follower(targa_provvisoria("borderline_grow"))
    db = _FakeDb()
    assert asyncio.run(harvest_profile_into_follower(db, f, _payload(12345))) is True
    assert f.ig_user_id == 12345


def test_targa_gia_vera_e_uguale_resta_invariata():
    f = _follower(12345)
    db = _FakeDb()
    asyncio.run(harvest_profile_into_follower(db, f, _payload(12345)))
    assert f.ig_user_id == 12345


def test_identita_cambiata_non_sovrascrive_la_scheda():
    """Handle riassegnato: si e' gia' mandato il DM (scelta di Tommaso), ma i dati
    dello sconosciuto NON finiscono sulla scheda del contatto del cliente."""
    f = _follower(12345)
    f.biography = "bio del contatto vero"
    db = _FakeDb()
    asyncio.run(harvest_profile_into_follower(db, f, _payload(99999)))
    assert f.ig_user_id == 12345                    # targa non toccata
    assert f.biography == "bio del contatto vero"   # scheda non sporcata
    assert f.skip_reason == "handle_riassegnato"    # ma evidente


def test_targa_vera_gia_su_altra_riga_non_fonde():
    """UniqueConstraint(campaign_id, ig_user_id): scrivere qui solleverebbe.
    Stessa scelta di browser_bio: skip + segnalazione, mai un merge indovinato."""
    f = _follower(targa_provvisoria("borderline_grow"))
    db = _FakeDb(altre_righe=[SimpleNamespace(id="f2", username="altro")])
    asyncio.run(harvest_profile_into_follower(db, f, _payload(12345)))
    assert f.ig_user_id != 12345
    assert f.skip_reason == "targa_gia_presente_su_altra_riga"


def test_payload_senza_pk_non_rompe_nulla():
    f = _follower(targa_provvisoria("borderline_grow"))
    db = _FakeDb()
    asyncio.run(harvest_profile_into_follower(db, f, {"username": "borderline_grow"}))
    assert f.ig_user_id == targa_provvisoria("borderline_grow")


def test_non_solleva_mai():
    """Contratto del modulo: gira dopo 'sent', un guasto non tocca l'invio."""
    f = _follower(targa_provvisoria("x"))
    class _Esplode:
        async def commit(self): raise RuntimeError("db giu'")
        async def rollback(self): pass
        async def execute(self, _s): raise RuntimeError("db giu'")
    assert asyncio.run(harvest_profile_into_follower(_Esplode(), f, _payload(1))) is False


def test_corsa_persa_sulla_targa_non_solleva_e_lo_dice():
    """La SELECT preventiva esclude la collisione quando guarda, ma fra quella
    lettura e il commit un altro worker puo' prendere la stessa targa: la finestra
    e' reale e resta aperta di proposito (chiuderla con un savepoint costerebbe un
    lazy load ORM dentro il gestore d'errore).

    Cio' che si pretende e' che il motivo sia DISTINGUIBILE nei log. Asserire solo
    il ritorno False non misurerebbe nulla: senza il ramo dedicato lo prenderebbe
    comunque l'`except Exception` generico, e il test resterebbe verde dicendo
    "scrittura saltata" — lo stesso messaggio di un DB irraggiungibile.
    """
    from loguru import logger as _logger
    from sqlalchemy.exc import IntegrityError

    f = _follower(targa_provvisoria("borderline_grow"))

    class _CorsaPersa(_FakeDb):
        async def commit(self):
            raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))

    righe = []
    sink = _logger.add(righe.append, level="ERROR")
    try:
        esito = asyncio.run(harvest_profile_into_follower(_CorsaPersa(), f, _payload(12345)))
    finally:
        _logger.remove(sink)

    assert esito is False                                  # il contratto regge
    assert any("targa non ancorata" in r for r in righe), righe
