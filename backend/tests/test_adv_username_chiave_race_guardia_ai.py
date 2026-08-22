"""ADVERSARIALE — la guardia AI-senza-bio (`valida_ai_senza_bio`, models/campaign.py)
si aggira con due PUT concorrenti sulla stessa campagna.

`update_campaign` (app/api/campaigns.py:242) legge la campagna con una SELECT
semplice (_get_or_404, nessun `with_for_update`), muta in memoria SOLO il campo
che il payload tocca, valida `campaign.ai_enabled` + `campaign.enrichment_level`
COSI' COME STANNO NELL'OGGETTO ORM DI QUELLA RICHIESTA, e committa. Se due
richieste leggono la campagna PRIMA che l'altra abbia committato, ciascuna vede
la combinazione vecchia (valida) dell'altro campo — non quella che l'altra
richiesta sta per scrivere — quindi ciascuna supera la guardia individualmente.
Il risultato in DB dopo entrambi i commit puo' essere la combinazione VIETATA
(ai_enabled=True, enrichment_level='none') che ne' l'uno ne' l'altro PUT, preso
da solo, avrebbe mai lasciato passare.

La finestra reale non ha bisogno che i DUE COMMIT siano in un ordine preciso:
basta che le DUE LETTURE avvengano entrambe prima che l'una o l'altra scriva.
Da li' in poi ciascuna sessione modifica in memoria SOLO la colonna che il suo
payload tocca, e SQLAlchemy genera un UPDATE con le sole colonne "sporche" di
quella sessione — quindi qualunque sia l'ordine dei due commit, nessuno dei due
sovrascrive la colonna dell'altro: il risultato finale e' la combinazione
proibita, a prescindere da chi ha vinto la corsa al commit.

Il test forza questa finestra con un handshake (`asyncio.Event`) attorno alla
VERA `_get_or_404` usata da `update_campaign` — non riscrive la logica
dell'endpoint, la esegue davvero due volte in parallelo e si limita a garantire
che nessuna delle due letture "salti avanti" oltre il commit dell'altra, cosa
che con `asyncio.gather` puro (provato per primo) NON succedeva in modo
affidabile: sqlite via aiosqlite risolve la SELECT cosi' in fretta che la prima
richiesta arriva quasi sempre al commit prima che la seconda legga, mascherando
la corsa. Sotto un vero Postgres condiviso, con latenza di rete reale fra due
processi worker diversi, la stessa finestra si apre da sola.
"""
import asyncio
import os
import tempfile
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Registra tutte le tabelle ORM su Base.metadata (stesso rituale di
# test_guardia_ai_senza_bio.py: senza questi import create_all non le vede).
import app.models.account  # noqa: F401
import app.models.activity_log  # noqa: F401
import app.models.campaign_account  # noqa: F401
import app.models.follower  # noqa: F401
import app.models.global_contact  # noqa: F401
import app.models.imported_profile  # noqa: F401
import app.models.message  # noqa: F401
import app.models.user  # noqa: F401

from app.database import Base
from app.models.campaign import Campaign, CampaignStatus, valida_ai_senza_bio
from app.schemas.campaign import CampaignUpdate


@pytest_asyncio.fixture
async def _sessioni():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="adv_race_guardia_ai_")
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
@pytest.mark.xfail(strict=True, reason=(
    """DIFETTO CONFERMATO, non ancora chiuso: serve una decisione, non una patch.

    Due PUT concorrenti — uno che accende l'AI, uno che abbassa il livello a
    'none' — superano entrambi la guardia se leggono prima che l'altro
    committi: ciascuno vede l'altro asse ancora al valore vecchio, valido.
    SQLAlchemy scrive solo le colonne sporche, quindi nessuno sovrascrive
    l'altro e il DB finisce nella combinazione vietata senza che nessuna delle
    due richieste l'abbia mai vista.

    Il rosso qui si riproduce solo forzando l'interleaving: su sqlite la SELECT
    e' troppo veloce. Sotto Postgres, due processi worker con latenza di rete
    reale, la finestra si apre da sola.

    Perche' non lo correggo qui: la cura e' un lock di riga (`with_for_update`)
    in `_get_or_404`, che e' condivisa da TUTTI gli endpoint campagne — non e'
    una modifica da fare a fine serata senza misurarne l'effetto sugli altri
    percorsi. L'alternativa (un CHECK constraint in DB) oggi non e' applicabile:
    esiste gia' una campagna in produzione in quello stato, e il vincolo la
    rifiuterebbe. strict=True: torna verde da solo quando il lock arriva."""
))
async def test_due_put_concorrenti_scrivono_la_combinazione_vietata(_sessioni):
    import app.api.campaigns as campaigns_mod
    from app.api.campaigns import update_campaign

    session_factory = _sessioni

    # Stato iniziale: valido su ENTRAMBI gli assi (ai_enabled=False, bio) —
    # esattamente il punto di partenza che una campagna reale avrebbe.
    async with session_factory() as db_setup:
        campagna = Campaign(
            name="adv-race-ai",
            target_username="bersaglio",
            source_type="scrape",
            base_message_template="Ciao, ti va di sentirci?",
            messaging_enabled=True,
            ai_enabled=False,
            enrichment_level="bio",
            status=CampaignStatus.draft,
        )
        db_setup.add(campagna)
        await db_setup.commit()
        cid = campagna.id

    # Handshake attorno alla VERA _get_or_404 di app/api/campaigns.py: chi legge
    # PER PRIMO aspetta che anche l'altra richiesta abbia letto (quindi PRIMA di
    # qualunque commit) prima di proseguire verso validazione+commit. Non serve
    # controllare l'ordine dei due commit (vedi docstring del modulo).
    stato = {"letture": 0}
    evento_seconda_lettura = asyncio.Event()
    originale_get_or_404 = campaigns_mod._get_or_404

    async def _get_or_404_in_staffetta(campaign_id, db):
        campagna_letta = await originale_get_or_404(campaign_id, db)
        stato["letture"] += 1
        if stato["letture"] == 1:
            await evento_seconda_lettura.wait()
        else:
            evento_seconda_lettura.set()
        return campagna_letta

    campaigns_mod._get_or_404 = _get_or_404_in_staffetta

    # Le due richieste concorrenti: A accende l'AI, B abbassa il livello a
    # 'none'. Prese singolarmente sono entrambe legittime (l'altro asse, nella
    # foto che ciascuna legge all'inizio, e' ancora quello vecchio e valido).
    async def richiesta_a():
        async with session_factory() as db:
            return await update_campaign(cid, CampaignUpdate(ai_enabled=True), db=db)

    async def richiesta_b():
        async with session_factory() as db:
            return await update_campaign(cid, CampaignUpdate(enrichment_level="none"), db=db)

    try:
        esito_a, esito_b = await asyncio.gather(
            richiesta_a(), richiesta_b(), return_exceptions=True
        )
    finally:
        campaigns_mod._get_or_404 = originale_get_or_404

    # Nessuna delle due richieste, presa da sola, doveva essere rifiutata: sono
    # entrambe individualmente valide contro lo stato che leggono.
    from fastapi import HTTPException
    for esito, nome in ((esito_a, "A (ai_enabled=True)"), (esito_b, "B (enrichment_level=none)")):
        assert not isinstance(esito, HTTPException), f"{nome} rifiutata da sola: {esito.detail}"
        assert not isinstance(esito, Exception), f"{nome} ha sollevato: {esito!r}"

    async with session_factory() as db_check:
        riga = (await db_check.execute(
            select(Campaign.ai_enabled, Campaign.enrichment_level).where(Campaign.id == cid)
        )).one()
        ai_enabled_finale, livello_finale = riga

    # LA GUARDIA E' STATA AGGIRATA: la combinazione finale in DB e' quella che
    # valida_ai_senza_bio dichiara vietata, eppure nessuna delle due richieste
    # l'ha mai vista né validata come tale.
    errore = valida_ai_senza_bio(ai_enabled_finale, livello_finale)
    assert errore is None, (
        f"la guardia AI-senza-bio e' stata aggirata da due PUT concorrenti: "
        f"stato finale ai_enabled={ai_enabled_finale!r} enrichment_level={livello_finale!r} "
        f"-> {errore!r}. Una campagna con l'AI accesa e senza bio e' ora RUNNING."
    )
