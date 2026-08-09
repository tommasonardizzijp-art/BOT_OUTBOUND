"""Dedup per username, fusione, precedenza di stato.

La fusione non e' un caso limite: e' l'esito NORMALE, perche' ogni contatto
raccolto via API ha full_name=None e verra' riaperto.
"""
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.campaign import Campaign
from app.models.follower import Follower, FollowerStatus
from app.services.inbox_browser.salvataggio import (
    DatiContatto, salva_contatto, stato_vincente,
)
from app.services.inbox_browser.targa import targa_provvisoria


@pytest_asyncio.fixture
async def campagna(db_session):
    c = Campaign(name="test inbox browser")
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    return c


def _dati(username="lerocchette", nome="Elena Rocchetti", testo="ciao"):
    return DatiContatto(
        username=username,
        nome=nome,
        last_message_at=datetime(2026, 8, 1, 12, 0),
        last_message_from="them",
        last_message_text=testo,
    )


@pytest.mark.asyncio
async def test_primo_salvataggio_crea(db_session, campagna):
    esito = await salva_contatto(db_session, campagna.id, _dati())
    assert esito == "creato"
    f = (await db_session.execute(
        select(Follower).where(Follower.campaign_id == campagna.id)
    )).scalar_one()
    assert f.username == "lerocchette"
    assert f.ig_user_id == targa_provvisoria("lerocchette")
    assert f.source_channel == "browser"


@pytest.mark.asyncio
async def test_secondo_salvataggio_aggiorna_non_duplica(db_session, campagna):
    await salva_contatto(db_session, campagna.id, _dati(testo="primo"))
    esito = await salva_contatto(db_session, campagna.id, _dati(testo="secondo"))
    assert esito == "aggiornato"
    righe = (await db_session.execute(
        select(Follower).where(Follower.campaign_id == campagna.id)
    )).scalars().all()
    assert len(righe) == 1
    assert righe[0].last_message_text == "secondo"


@pytest.mark.asyncio
async def test_contatto_gia_raccolto_via_API_non_viene_duplicato(db_session, campagna):
    """Il caso che rende la fusione la norma: targa VERA gia' in DB, nessun nome."""
    db_session.add(Follower(
        campaign_id=campagna.id, ig_user_id=76561234567, username="lerocchette",
        full_name=None, status=FollowerStatus.pending, source_channel="api",
    ))
    await db_session.commit()

    esito = await salva_contatto(db_session, campagna.id, _dati())
    assert esito == "aggiornato"
    righe = (await db_session.execute(
        select(Follower).where(Follower.campaign_id == campagna.id)
    )).scalars().all()
    assert len(righe) == 1, "una riga duplicata qui puo' portare a un SECONDO DM"
    assert righe[0].ig_user_id == 76561234567, "la targa vera non si sovrascrive con una provvisoria"
    assert righe[0].full_name == "Elena Rocchetti", "il nome mancante viene riempito"


@pytest.mark.asyncio
async def test_uno_stato_avanzato_non_torna_indietro(db_session, campagna):
    """Un contatto gia' contattato NON deve tornare mandabile."""
    db_session.add(Follower(
        campaign_id=campagna.id, ig_user_id=76561234567, username="lerocchette",
        status=FollowerStatus.sent, source_channel="api",
    ))
    await db_session.commit()

    await salva_contatto(db_session, campagna.id, _dati())
    f = (await db_session.execute(
        select(Follower).where(Follower.campaign_id == campagna.id)
    )).scalar_one()
    assert f.status == FollowerStatus.sent, "un sent tornato pending riceve un secondo DM"


@pytest.mark.asyncio
async def test_username_normalizzato_nel_confronto(db_session, campagna):
    await salva_contatto(db_session, campagna.id, _dati(username="lerocchette"))
    esito = await salva_contatto(db_session, campagna.id, _dati(username="LeRocchette"))
    assert esito == "aggiornato"


@pytest.mark.asyncio
async def test_username_vuoto_solleva(db_session, campagna):
    with pytest.raises(ValueError):
        await salva_contatto(db_session, campagna.id, _dati(username="  "))


def test_precedenza_di_stato():
    assert stato_vincente(FollowerStatus.pending, FollowerStatus.sent) == FollowerStatus.sent
    assert stato_vincente(FollowerStatus.sent, FollowerStatus.pending) == FollowerStatus.sent
    assert stato_vincente(FollowerStatus.pending, FollowerStatus.pending) == FollowerStatus.pending
