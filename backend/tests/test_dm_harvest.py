# backend/tests/test_dm_harvest.py
"""L'harvest non deve mai danneggiare l'invio ne' i dati gia' raccolti."""
import asyncio
from types import SimpleNamespace

from app.services.dm_harvest import harvest_profile_into_follower

PAYLOAD = {
    "username": "mario_rossi", "pk": "123", "full_name": "Mario Rossi",
    "biography": "Titolare del negozio", "follower_count": 500,
    "following_count": 200, "is_private": False, "is_verified": False,
    "account_type": 2, "external_url": None, "bio_links": [],
}


class _FakeDb:
    def __init__(self, esplode=False):
        self.commits = 0
        self._esplode = esplode

    async def commit(self):
        if self._esplode:
            raise RuntimeError("DB giu'")
        self.commits += 1

    async def rollback(self):
        pass


def _follower(**kw):
    base = dict(id="f1", username="mario_rossi", full_name=None, biography=None,
                follower_count=None, following_count=None, is_private=False,
                is_verified=False, external_url=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_riempie_i_campi_vuoti():
    f = _follower()
    db = _FakeDb()
    assert asyncio.run(harvest_profile_into_follower(db, f, PAYLOAD)) is True
    assert f.biography == "Titolare del negozio"
    assert f.full_name == "Mario Rossi"
    assert f.follower_count == 500
    assert db.commits == 1


def test_non_sovrascrive_dati_gia_raccolti():
    f = _follower(biography="bio dalla Fase Bio", full_name="Nome Vero")
    asyncio.run(harvest_profile_into_follower(_FakeDb(), f, PAYLOAD))
    assert f.biography == "bio dalla Fase Bio"
    assert f.full_name == "Nome Vero"


def test_payload_assente_non_fa_niente():
    f = _follower()
    db = _FakeDb()
    assert asyncio.run(harvest_profile_into_follower(db, f, None)) is False
    assert db.commits == 0


def test_un_errore_del_db_non_solleva():
    # L'harvest gira DOPO che il DM e' partito: un suo guasto non deve mai
    # propagarsi al chiamante, che ha gia' marcato il follower come 'sent'.
    f = _follower()
    assert asyncio.run(harvest_profile_into_follower(_FakeDb(esplode=True), f, PAYLOAD)) is False


def test_payload_spazzatura_non_solleva():
    f = _follower()
    for spazzatura in ({}, {"username": None}, {"biography": 12345}):
        assert asyncio.run(harvest_profile_into_follower(_FakeDb(), f, spazzatura)) in (True, False)
