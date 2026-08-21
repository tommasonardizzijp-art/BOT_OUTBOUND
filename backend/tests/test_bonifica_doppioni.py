"""Lo script di bonifica e' l'unico pezzo del pacchetto che CANCELLA righe in
produzione: le sue guardie si testano.

Cosa deve garantire:
  - fonde solo la coppia attesa (una targa provvisoria + una reale, stesso username);
  - tiene la riga del browser (full_name, last_message_*) e le scrive il pk vero;
  - non cancella niente se la riga da promuovere e' cambiata sotto (rowcount != 1);
  - lascia stare le coppie in cui la riga da cancellare ha una storia.
"""
import asyncio
import importlib.util
import os
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models.account          # noqa: F401 — registra le tabelle nei metadata
import app.models.campaign_account  # noqa: F401
import app.models.message          # noqa: F401
import app.models.activity_log     # noqa: F401
import app.models.global_contact   # noqa: F401
from app.database import Base
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.models.message import Message

# Lo script vive in scripts/ e non e' un modulo importabile: si carica dal path.
_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bonifica_doppioni_targa_provvisoria.py"
_spec = importlib.util.spec_from_file_location("bonifica_doppioni", _SCRIPT)
bonifica = importlib.util.module_from_spec(_spec)
sys.modules["bonifica_doppioni"] = bonifica
_spec.loader.exec_module(bonifica)


@pytest.fixture
def db_factory():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="bonifica_")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _crea():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_crea())
    yield factory
    asyncio.run(engine.dispose())
    try:
        os.remove(path)
    except OSError:
        pass


def _campagna_con_coppia(factory, **extra_api):
    """Una campagna con la coppia tipica: riga browser provvisoria + gemella API."""
    cid = str(uuid.uuid4())
    ids = {}

    async def _go():
        async with factory() as db:
            db.add(Campaign(id=cid, name="bonifica test", source_type="scrape",
                            scrape_mode="dm_threads", status=CampaignStatus.paused,
                            messaging_enabled=False))
            browser = Follower(
                campaign_id=cid, ig_user_id=-8347, username="mario_shop",
                full_name="Mario", source_channel="browser",
                last_message_text="ciao", status=FollowerStatus.pending,
            )
            api = Follower(
                campaign_id=cid, ig_user_id=555, username="mario_shop",
                status=FollowerStatus.pending, **extra_api,
            )
            db.add_all([browser, api])
            await db.commit()
            ids["browser"] = browser.id
            ids["api"] = api.id

    asyncio.run(_go())
    return cid, ids


def _righe(factory, cid):
    async def _go():
        async with factory() as db:
            return (await db.execute(
                select(Follower).where(Follower.campaign_id == cid)
            )).scalars().all()
    return asyncio.run(_go())


def test_fonde_tenendo_la_riga_del_browser(db_factory):
    cid, ids = _campagna_con_coppia(db_factory)

    async def _go():
        async with db_factory() as db:
            coppie, scartate = await bonifica._coppie(db, cid)
            assert scartate == []
            assert len(coppie) == 1
            _, _, browser_row, api_row = coppie[0]
            assert await bonifica._api_e_una_scheda_vuota(db, api_row) is None
            return await bonifica.fondi_coppia(db, browser_row, api_row)

    assert asyncio.run(_go()) == "fusa"
    righe = _righe(db_factory, cid)
    assert len(righe) == 1
    assert righe[0].id == ids["browser"], "deve sopravvivere la riga del browser"
    assert righe[0].ig_user_id == 555, "con il pk vero della gemella"
    assert righe[0].full_name == "Mario"
    assert righe[0].last_message_text == "ciao"


def test_non_cancella_se_la_riga_da_promuovere_e_cambiata_sotto(db_factory):
    """Corsa con la Fase Bio: se la riga browser non e' piu' provvisoria, la UPDATE
    non tocca niente. Senza il controllo sul rowcount la DELETE passerebbe lo stesso
    e il contatto sparirebbe insieme al suo pk."""
    cid, ids = _campagna_con_coppia(db_factory)

    async def _go():
        async with db_factory() as db:
            coppie, _ = await bonifica._coppie(db, cid)
            _, _, browser_row, api_row = coppie[0]
            # qualcun altro promuove la riga browser mentre lo script gira
            altra = await db.get(Follower, ids["browser"])
            altra.ig_user_id = 999
            await db.commit()
            return await bonifica.fondi_coppia(db, browser_row, api_row)

    esito = asyncio.run(_go())
    assert esito != "fusa"
    righe = _righe(db_factory, cid)
    assert len(righe) == 2, "nessuna delle due righe deve sparire"


@pytest.mark.parametrize("campo,valore", [
    ("full_name", "Qualcuno"),
    ("biography", "bio gia' presa"),
    ("phone", "+391112223334"),
    ("source_channel", "browser"),
])
def test_salta_la_coppia_se_la_gemella_ha_una_storia(db_factory, campo, valore):
    """La gemella da cancellare deve essere una scheda VUOTA. Se ha dei dati, le due
    righe non sono quello che lo script crede e la coppia va guardata a mano."""
    cid, _ = _campagna_con_coppia(db_factory, **{campo: valore})

    async def _go():
        async with db_factory() as db:
            coppie, _ = await bonifica._coppie(db, cid)
            _, _, _, api_row = coppie[0]
            return await bonifica._api_e_una_scheda_vuota(db, api_row)

    motivo = asyncio.run(_go())
    assert motivo is not None and campo in motivo


def test_salta_la_coppia_se_la_gemella_ha_messaggi(db_factory):
    """La FK messages.follower_id e' ON DELETE CASCADE: cancellare la riga
    cancellerebbe anche i messaggi."""
    cid, ids = _campagna_con_coppia(db_factory)

    async def _go():
        async with db_factory() as db:
            db.add(Message(campaign_id=cid, follower_id=ids["api"], generated_text="ciao"))
            await db.commit()
            coppie, _ = await bonifica._coppie(db, cid)
            _, _, _, api_row = coppie[0]
            return await bonifica._api_e_una_scheda_vuota(db, api_row)

    motivo = asyncio.run(_go())
    assert motivo is not None and "messaggi" in motivo


def test_tre_righe_sullo_stesso_username_non_sono_una_coppia(db_factory):
    """Mai indovinare: se il gruppo non e' esattamente 1 provvisoria + 1 reale, si
    riporta e si lascia stare."""
    cid, _ = _campagna_con_coppia(db_factory)

    async def _go():
        async with db_factory() as db:
            db.add(Follower(campaign_id=cid, ig_user_id=777, username="Mario_Shop",
                            status=FollowerStatus.pending))
            await db.commit()
            return await bonifica._coppie(db, cid)

    coppie, scartate = asyncio.run(_go())
    assert coppie == []
    assert len(scartate) == 1 and "non e' la coppia attesa" in scartate[0][2]


def test_non_cancella_se_la_gemella_cambia_fra_il_controllo_e_la_cancellazione(db_factory):
    """Le guardie del pre-check si ripetono DENTRO la transazione: fra la lettura e
    la DELETE un worker puo' portare la riga a 'sent' o scriverle un dato, e a quel
    punto non e' piu' la scheda vuota che lo script credeva di cancellare."""
    cid, ids = _campagna_con_coppia(db_factory)

    async def _go():
        async with db_factory() as db:
            coppie, _ = await bonifica._coppie(db, cid)
            _, _, browser_row, api_row = coppie[0]
            async with db_factory() as altra:      # un worker, nel frattempo
                riga = await altra.get(Follower, ids["api"])
                riga.status = FollowerStatus.sent
                await altra.commit()
            return await bonifica.fondi_coppia(db, browser_row, api_row)

    esito = asyncio.run(_go())
    assert esito != "fusa" and "cambiata sotto" in esito
    righe = _righe(db_factory, cid)
    assert len(righe) == 2, "nessuna delle due righe deve sparire"
    assert sorted(r.ig_user_id for r in righe) == [-8347, 555], "e nessuna deve cambiare targa"


def test_non_cancella_se_nel_frattempo_arriva_un_messaggio(db_factory):
    """messages.follower_id e' ON DELETE CASCADE e il backup contiene solo i
    Follower: un messaggio arrivato dopo il controllo sparirebbe senza copia."""
    cid, ids = _campagna_con_coppia(db_factory)

    async def _go():
        async with db_factory() as db:
            coppie, _ = await bonifica._coppie(db, cid)
            _, _, browser_row, api_row = coppie[0]
            async with db_factory() as altra:
                altra.add(Message(campaign_id=cid, follower_id=ids["api"],
                                  generated_text="ciao"))
                await altra.commit()
            return await bonifica.fondi_coppia(db, browser_row, api_row)

    esito = asyncio.run(_go())
    assert esito != "fusa"
    assert len(_righe(db_factory, cid)) == 2
