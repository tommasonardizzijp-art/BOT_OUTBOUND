"""E2E — il giro completo della lista inbox API con contatti misti.

Riusa l'harness di tests/test_scrape_inbox_adversarial.py (servizio + DB SQLite
vero, sorgente scriptata) per far scendere in UNA pagina: un contatto vero nuovo,
un contatto gia' presente con targa provvisoria (deve essere PROMOSSO, non
duplicato), due profili chiusi col segnaposto (non devono produrre righe), e uno
username gia' presente con targa REALE diversa (rename: due persone, si inserisce
e si segnala).

Ogni test rompe deliberatamente la riga di produzione sotto assert e verifica il
rosso, poi ripristina.
"""
import asyncio

from sqlalchemy import select

from app.models.campaign import Campaign
from app.models.follower import Follower, FollowerStatus
from app.services.inbox_source import InboxPage
from test_scrape_inbox_adversarial import _run_inbox_list, _setup_inbox_db  # noqa: E402


def _semina(session_factory, campaign_id, **kwargs):
    async def _go():
        async with session_factory() as db:
            db.add(Follower(campaign_id=campaign_id, **kwargs))
            await db.commit()
    asyncio.run(_go())


def _leggi(session_factory, campaign_id):
    async def _go():
        async with session_factory() as db:
            righe = (await db.execute(
                select(Follower).where(Follower.campaign_id == campaign_id)
            )).scalars().all()
            campaign = await db.get(Campaign, campaign_id)
            return righe, campaign
    return asyncio.run(_go())


# Il segnaposto reale osservato in log (22/08): "Utente di Instagram" contiene
# uno spazio, handle_valido lo scarta per forma. Un secondo, diverso, in un'altra
# lingua, per dimostrare che non si affida al lessico.
SEGNAPOSTO_IT = "Utente di Instagram"
SEGNAPOSTO_ALTRA_LINGUA = "Instagram utilisateur"


def test_pagina_mista_promuove_scarta_segnaposto_e_segnala_collisione(monkeypatch):
    pagina = [
        (701, "nuovo_vero"),                 # nuovo
        (555, "mario_shop"),                 # promozione (in DB a targa -8347)
        (9001, SEGNAPOSTO_IT),               # segnaposto: scartato
        (9002, SEGNAPOSTO_ALTRA_LINGUA),     # segnaposto: scartato
        (999, "negozio"),                    # collisione: in DB "negozio"->111 (targa reale diversa)
    ]
    pages = [
        InboxPage(participants=pagina, cursor="c1", exhausted=False),
        InboxPage(participants=[], cursor=None, exhausted=True, bottom_confirmed=True),
    ]
    session_factory, campaign_id, cleanup = _setup_inbox_db(monkeypatch, pages)
    try:
        _semina(session_factory, campaign_id, ig_user_id=-8347, username="mario_shop",
                full_name="Mario", status=FollowerStatus.pending, source_channel="browser")
        _semina(session_factory, campaign_id, ig_user_id=111, username="negozio",
                status=FollowerStatus.pending, source_channel="api")

        _run_inbox_list(session_factory, campaign_id)
        righe, campaign = _leggi(session_factory, campaign_id)
        per_pk = {r.ig_user_id: r for r in righe}

        # 4 righe attese: 701 nuovo, 555 promosso (sostituisce -8347), 111 intatto,
        # 999 inserito come persona diversa. I due segnaposto (9001, 9002) NON
        # devono comparire.
        assert sorted(per_pk.keys()) == [111, 555, 701, 999], (
            f"righe inattese o mancanti: {sorted(per_pk.keys())}"
        )
        assert 9001 not in per_pk and 9002 not in per_pk, "un segnaposto ha prodotto una riga"
        assert per_pk[555].full_name == "Mario", "la promozione ha perso i dati della riga browser"
        assert per_pk[555].source_channel == "browser"
        assert per_pk[999].username == "negozio"
        assert campaign.total_followers == 4, f"contatore sballato: {campaign.total_followers}"
    finally:
        cleanup()
