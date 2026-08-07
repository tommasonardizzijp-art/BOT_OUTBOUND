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


class _FollowerParanoico:
    """Follower i cui attributi diventano illeggibili dopo che il DB e' 'esploso' -
    riproduce lo stato 'pending rollback' di una sessione SQLAlchemy reale: dopo
    un commit fallito, rileggere un attributo ORM (es. .username) fa risalire
    PendingRollbackError. Serve per la regressione adversarial (Test 11)."""

    def __init__(self, **kw):
        object.__setattr__(self, "_dati", dict(kw))
        object.__setattr__(self, "_bloccato", False)

    def __getattr__(self, name):
        if object.__getattribute__(self, "_bloccato"):
            raise RuntimeError(
                "PendingRollbackError: this Session's transaction has been "
                "rolled back due to a previous exception during flush."
            )
        return object.__getattribute__(self, "_dati").get(name)

    def __setattr__(self, name, value):
        object.__getattribute__(self, "_dati")[name] = value

    def blocca(self):
        object.__setattr__(self, "_bloccato", True)


class _FakeDbCommitEsplodeEBlocca:
    """Come un commit reale che fallisce per un valore fuori range (int64): il
    commit solleva, e la sessione (qui simulata sul follower stesso) smette di
    poter essere riletta finche' non arriva un rollback."""

    def __init__(self, follower):
        self._follower = follower

    async def commit(self):
        self._follower.blocca()
        raise RuntimeError("integer out of range for int64")

    async def rollback(self):
        pass


def test_log_di_errore_non_rilegge_follower_dopo_commit_fallito():
    """Adversarial Test 11: se il commit fallisce, la sessione entra in stato
    'pending rollback'. Il log difensivo nel blocco except NON deve rileggere un
    attributo ORM del follower a quel punto (es. .username per il messaggio di
    log), o fa risalire l'eccezione della sessione al chiamante -- che ha GIA'
    marcato il DM come inviato. Il contratto e' 'non solleva MAI'."""
    f = _FollowerParanoico(
        username="mario_rossi", full_name=None, biography=None,
        follower_count=None, following_count=None, is_private=False,
        is_verified=False, external_url=None,
    )
    db = _FakeDbCommitEsplodeEBlocca(f)
    assert asyncio.run(harvest_profile_into_follower(db, f, PAYLOAD)) is False


def test_is_private_stringa_non_forza_un_booleano_sbagliato():
    """Adversarial Test 30: web_user_to_shim fa bool(valore) sul payload grezzo.
    bool("false") e' True (bug classico di Python) -- quindi una stringa nel
    payload puo' far scrivere l'OPPOSTO del vero senza errore ne' log. La guardia
    su isinstance(nuovo, bool) in dm_harvest arriva troppo tardi: nuovo e' gia'
    un bool prodotto dallo shim. Deve validare il TIPO sul payload grezzo."""
    f = _follower(is_private=False)
    payload_stringa = dict(PAYLOAD, is_private="false")  # il caso peggiore
    asyncio.run(harvest_profile_into_follower(_FakeDb(), f, payload_stringa))
    assert f.is_private is False
