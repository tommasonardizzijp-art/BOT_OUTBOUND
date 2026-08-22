"""ADVERSARIALE — `harvest_profile_into_follower` (app/services/dm_harvest.py)
promette "non solleva MAI" (docstring del modulo, riga 7). Rompo il contratto
trovando UN caso dove solleva comunque.

Il modulo gia' si difende dal caso "il COMMIT fallisce dentro questa funzione e
lascia la sessione in pending-rollback" (vedi tests/test_dm_harvest.py,
`test_log_di_errore_non_rilegge_follower_dopo_commit_fallito`): per questo la
riga 29 cattura `username = getattr(follower, "username", "?")` PRIMA di
entrare nel `try`, cosi' il log nel blocco `except` non rilegge un attributo
ORM quando la sessione e' gia' rotta.

Ma quella stessa riga 29 e' FUORI dal try — e non e' protetta da niente. Se il
follower arriva GIA' in uno stato in cui leggere `.username` solleva (sessione
gia' in pending-rollback PRIMA che questa funzione venga chiamata: un guasto
precedente nella stessa richiesta, non necessariamente causato da questa
funzione), quella riga fa risalire l'eccezione al chiamante — la stessa
`PendingRollbackError` che il codice sostiene di aver escluso.

Il difetto e' in `getattr(obj, name, default)`: il `default` copre SOLO
`AttributeError`. Qualunque altra eccezione sollevata dall'accesso
all'attributo (compresa `sqlalchemy.exc.PendingRollbackError`, che NON eredita
da `AttributeError`) attraversa `getattr` senza essere fermata.
"""
import asyncio

import pytest

from app.services.dm_harvest import harvest_profile_into_follower

PAYLOAD = {
    "username": "mario_rossi", "pk": "123", "full_name": "Mario Rossi",
    "biography": "Titolare del negozio", "follower_count": 500,
    "following_count": 200, "is_private": False, "is_verified": False,
    "account_type": 2, "external_url": None, "bio_links": [],
}


class _FollowerGiaEsploso:
    """Simula una sessione ORM GIA' in pending-rollback PRIMA che
    harvest_profile_into_follower venga chiamata (non a causa sua): qualunque
    accesso ad attributo, incluso il PRIMISSIMO (`.username` a dm_harvest.py:29),
    solleva — esattamente come farebbe un oggetto ORM scaduto su una sessione
    rotta da un guasto avvenuto PRIMA in questa stessa richiesta."""

    def __getattr__(self, name):
        raise RuntimeError(
            "PendingRollbackError: this Session's transaction has been "
            "rolled back due to a previous exception during flush."
        )


class _FakeDb:
    async def commit(self):
        pass

    async def rollback(self):
        pass


def test_follower_gia_in_pending_rollback_rompe_il_non_solleva_mai():
    f = _FollowerGiaEsploso()
    db = _FakeDb()
    # Il contratto e' "non solleva MAI": se questa chiamata solleva invece di
    # ritornare False, il contratto e' rotto. Verifico ESEGUENDO, non deducendo.
    try:
        esito = asyncio.run(harvest_profile_into_follower(db, f, PAYLOAD))
    except Exception as e:
        pytest.fail(
            f"harvest_profile_into_follower ha sollevato {type(e).__name__}({e!r}) "
            "invece di ritornare False — contratto 'non solleva MAI' violato. "
            "Il chiamante gira DOPO che il DM e' partito: questa eccezione "
            "risalirebbe fino al worker di invio."
        )
    assert esito is False
