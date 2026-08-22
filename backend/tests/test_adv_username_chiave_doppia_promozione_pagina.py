"""ADVERSARIALE — direzione 6 del cantiere: `classifica_pagina` (scrape_inbox.py)
promuove sulla riga giusta la PRIMA volta che uno username ricorre in pagina, e
`promossi_in_pagina` esiste apposta per non promuoverlo una seconda volta. Ma
la SECONDA occorrenza dello stesso username in pagina non viene scartata: va
in `esito.nuovi`, cioe' viene INSERITA come riga nuova — con lo stesso
username della riga appena promossa.

Se IG restituisce, nella STESSA pagina, due thread con lo stesso username ma
due pk diversi (caso reale: rename in corso, o cache incoerente fra due
thread), il risultato e' ESATTAMENTE il doppione che questo cantiere esiste
per eliminare — una riga promossa dalla targa provvisoria (pk vecchio/nuovo) e
una riga fresca (pk nuovo/vecchio), stesso username, due pk diversi, nella
stessa campagna.

Verificato prima come funzione pura (`classifica_pagina` produce sia una
promozione sia un inserimento per lo stesso username), poi end-to-end su
`run_inbox_list` con DB vero per dimostrare che finiscono davvero DUE righe.
"""
import asyncio
import pytest
import sys

sys.path.insert(0, "tests")

from sqlalchemy import select

from app.models.follower import Follower, FollowerStatus
from app.services.inbox_source import InboxPage
from app.services.inbox_browser.targa import targa_provvisoria
from app.services.scrape_inbox import classifica_pagina
from test_scrape_inbox_adversarial import _setup_inbox_db, _run_inbox_list  # noqa: E402


def test_pura_stesso_username_due_pk_produce_promozione_e_inserimento():
    targa_per_username = {"mario_shop": targa_provvisoria("mario_shop")}
    existing_ids = {targa_provvisoria("mario_shop")}
    participants = [(555, "mario_shop"), (777, "mario_shop")]

    esito = classifica_pagina(participants, existing_ids, targa_per_username)

    assert len(esito.promozioni) == 1 and esito.promozioni[0][0] == 555
    # QUESTO e' il difetto: la seconda occorrenza dello stesso username in
    # pagina non viene scartata ne' segnalata come collisione — diventa un
    # INSERIMENTO NUOVO con lo stesso username della riga appena promossa.
    assert (777, "mario_shop") in esito.nuovi, (
        "atteso (777, 'mario_shop') in esito.nuovi: se questo fallisce, la "
        "seconda occorrenza e' ora gestita diversamente e questo test va "
        "riletto, non solo aggiustato"
    )


@pytest.mark.xfail(strict=True, reason=(
    """DIFETTO CONFERMATO, non ancora chiuso: serve una decisione, non una patch.

    Lo stesso username che compare due volte nella STESSA pagina con due pk
    diversi produce due righe: una promossa, l'altra inserita come nuova
    (scrape_inbox.py, ramo `targa is None or u in promossi_in_pagina`).

    Il commento in produzione dice che sono "persone diverse". Su Instagram lo
    username e' unico in ogni istante, quindi due pk con lo stesso handle nella
    stessa fotografia non sono due persone: sono un rename in volo o una cache
    incoerente. Cioe' un doppione — esattamente quello che questo cantiere
    esiste per chiudere.

    Perche' non lo correggo qui: e' comportamento PREESISTENTE e deliberato, e
    cambiarlo cambia la semantica del dedup (il secondo va scartato e segnalato
    come collisione, non inserito). Va deciso e fatto nella Task 5/6, dove il
    dedup si tocca comunque. strict=True: quando quella scelta viene fatta,
    questo test torna verde da solo e pytest lo segnala come XPASS."""
))
def test_end_to_end_produce_due_righe_per_lo_stesso_username(monkeypatch):
    pagina = InboxPage(
        participants=[(555, "mario_shop"), (777, "mario_shop")],
        cursor=None, exhausted=True, bottom_confirmed=True,
        threads_letti=2, threads_con_utenti=2,
    )
    session_factory, campaign_id, cleanup = _setup_inbox_db(monkeypatch, [pagina])
    try:
        async def _semina():
            async with session_factory() as db:
                db.add(Follower(
                    campaign_id=campaign_id, ig_user_id=targa_provvisoria("mario_shop"),
                    username="mario_shop", status=FollowerStatus.pending,
                ))
                await db.commit()
        asyncio.run(_semina())

        _run_inbox_list(session_factory, campaign_id)

        async def _leggi():
            async with session_factory() as db:
                return (await db.execute(
                    select(Follower).where(
                        Follower.campaign_id == campaign_id,
                        Follower.username == "mario_shop",
                    )
                )).scalars().all()
        righe = asyncio.run(_leggi())

        assert len(righe) == 1, (
            f"{len(righe)} righe per 'mario_shop' nella stessa campagna "
            f"(pk: {sorted(r.ig_user_id for r in righe)}) — esattamente il "
            "doppione che username-come-chiave-di-prima-classe doveva chiudere. "
            "Nato da: stesso username due volte nella stessa pagina inbox con "
            "pk diversi, la seconda occorrenza finisce inserita invece di "
            "essere scartata o segnalata."
        )
    finally:
        cleanup()
