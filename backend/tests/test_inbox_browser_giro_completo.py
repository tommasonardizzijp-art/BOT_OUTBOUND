"""Regressioni trovate dal QA agent su Task 4a (integrazione reale del ciclo,
non solo le funzioni pure): due modi in cui `run_inbox_browser_list` perdeva
contatti in silenzio dopo l'introduzione della raccolta via `raccogli`.
"""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.services import scrape_inbox_browser
from app.services.inbox_browser.pagina import RigaVisibile


class _FakePage:
    def __init__(self):
        self.url = "https://www.instagram.com/direct/inbox/"

    def on(self, event, handler):
        pass

    async def goto(self, url, wait_until=None, timeout=None):
        return None

    async def wait_for_timeout(self, ms):
        return None

    async def evaluate(self, script, *args):
        return ""


class _FakeBrowserSession:
    pagina_condivisa = None

    def __init__(self, account_id):
        self.account_id = account_id
        self.context = SimpleNamespace(pages=[_FakeBrowserSession.pagina_condivisa])

    async def open(self):
        return None

    async def close(self):
        return None


async def _monta(monkeypatch, camp, righe_iniziali, decidi_fine_lista_fake, spia_apri=None,
                 apri_riga_fake=None):
    page = _FakePage()
    _FakeBrowserSession.pagina_condivisa = page
    monkeypatch.setattr("app.browser.context_manager.BrowserSession", _FakeBrowserSession)
    monkeypatch.setattr("app.utils.events.emit", lambda *a, **k: None)

    account = SimpleNamespace(id="acc-1", username="mio_account")

    async def fake_single_account(db_, campaign_id):
        return account
    monkeypatch.setattr(scrape_inbox_browser, "_single_inbox_account", fake_single_account)

    async def fake_leggi_righe(page_, lingua):
        return righe_iniziali
    monkeypatch.setattr(scrape_inbox_browser, "leggi_righe_visibili", fake_leggi_righe)
    monkeypatch.setattr(scrape_inbox_browser, "decidi_fine_lista", decidi_fine_lista_fake)
    monkeypatch.setattr(scrape_inbox_browser, "campiona_pausa", lambda zona: 0)

    async def fake_apri_riga(page_, indice, nome, lingua, account_username=None):
        if spia_apri is not None:
            spia_apri.append(nome)
        if apri_riga_fake is not None:
            return await apri_riga_fake(nome)
        return f"user_{normalizza_via_spia(nome)}"

    def normalizza_via_spia(nome):
        return "".join(c for c in (nome or "") if c.isalnum()) or "anon"

    monkeypatch.setattr(scrape_inbox_browser, "apri_riga", fake_apri_riga)

    return page


@pytest.mark.asyncio
async def test_nome_solo_emoji_non_viene_zittito_per_sempre(monkeypatch):
    """Bug trovato dal QA: `raccogli()` teneva una riga solo se
    `normalizza_nome(nome)` era truthy. Un nome fatto solo di emoji normalizza
    a stringa vuota, quindi non entrava MAI in `righe_del_giro` — zittita per
    tutta la sessione, esattamente il difetto che la memoria di sessione
    (Task 2) dichiara di non voler fare (vedi `gia_esaminata`: senza chiave si
    tratta sempre come nuova, mai come 'gia' vista')."""
    async with AsyncSessionLocal() as db:
        camp = Campaign(
            name="t-emoji-giro", status=CampaignStatus.listing,
            source_type="scrape", scrape_mode="dm_threads", inbox_engine="browser",
            list_target=1,
        )
        db.add(camp)
        await db.commit()
        await db.refresh(camp)

        riga_emoji = RigaVisibile(
            indice=0, nome="🎉🎉🎉", ultimo_nostro=None, non_letta=False,
            testo_grezzo="🎉🎉🎉\nultimo messaggio",
        )

        async def decidi_fine_lista_fake(page_, falliti, lingua, su_righe):
            return "fine"

        spia_apri: list = []
        await _monta(monkeypatch, camp, [riga_emoji], decidi_fine_lista_fake, spia_apri)

        await scrape_inbox_browser.run_inbox_browser_list(camp.id, db, camp)

        assert spia_apri == ["🎉🎉🎉"], (
            f"la riga a nome solo-emoji doveva essere tentata almeno una volta, spia={spia_apri}"
        )


@pytest.mark.asyncio
async def test_righe_scoperte_nel_gesto_di_fine_lista_non_si_perdono(monkeypatch):
    """Bug trovato dal QA (riprodotto 1 volta su 5 run con 150 contatti,
    persi in blocco gli ultimi 29): `decidi_fine_lista` campiona righe nuove
    DURANTE lo stesso scroll in cui puo' decidere 'fine'. Se il ciclo esce
    subito su 'fine' senza riconsumare `righe_del_giro`, quelle righe restano
    in una variabile locale che sparisce col `return` — mai processate."""
    async with AsyncSessionLocal() as db:
        camp = Campaign(
            name="t-coda-fine-giro", status=CampaignStatus.listing,
            source_type="scrape", scrape_mode="dm_threads", inbox_engine="browser",
        )
        db.add(camp)
        await db.commit()
        await db.refresh(camp)

        riga_coda = RigaVisibile(
            indice=0, nome="Ultimo Della Lista", ultimo_nostro=None, non_letta=False,
            testo_grezzo="Ultimo Della Lista\nciao",
        )

        chiamate = {"n": 0}

        async def decidi_fine_lista_fake(page_, falliti, lingua, su_righe):
            chiamate["n"] += 1
            if chiamate["n"] == 1:
                # Il gesto che scopre la fine della lista campiona anche
                # l'ultima riga rimasta, mai vista prima.
                await su_righe([riga_coda])
            return "fine"

        spia_apri: list = []
        # Nessuna riga iniziale nel DOM: si arriva subito a decidi_fine_lista.
        await _monta(monkeypatch, camp, [], decidi_fine_lista_fake, spia_apri)

        await scrape_inbox_browser.run_inbox_browser_list(camp.id, db, camp)

        assert spia_apri == ["Ultimo Della Lista"], (
            f"la riga scoperta nel gesto di 'fine' doveva essere processata, spia={spia_apri}"
        )


@pytest.mark.asyncio
async def test_apertura_fallita_non_cancella_la_riga_per_tutta_la_sessione(monkeypatch):
    """Root cause dei contatti persi in silenzio (misurata l'11/08: 84 righe su
    143 nella baseline). Il motore metteva la riga nella memoria di sessione
    PRIMA di provare ad aprirla: se l'apertura falliva — quasi sempre perche' la
    riga era uscita dal DOM mentre si scendeva, non per un problema della riga —
    quella chat risultava "gia' esaminata" per il resto della sessione e non
    veniva piu' ritentata da nessuna parte. Un fallimento di apertura non e' un
    giudizio sulla riga: va rimessa in gioco."""
    async with AsyncSessionLocal() as db:
        camp = Campaign(
            name="t-ritenta-riga", status=CampaignStatus.listing,
            source_type="scrape", scrape_mode="dm_threads", inbox_engine="browser",
        )
        db.add(camp)
        await db.commit()
        await db.refresh(camp)

        riga = RigaVisibile(
            indice=0, nome="Sfuggita Dal Dom", ultimo_nostro=None, non_letta=False,
            testo_grezzo="Sfuggita Dal Dom\nciao",
        )

        giri = {"n": 0}

        async def decidi_fine_lista_fake(page_, falliti, lingua, su_righe):
            giri["n"] += 1
            if giri["n"] == 1:
                # Il gesto successivo ri-incontra la stessa riga: se la memoria
                # di sessione l'ha gia' marcata, `raccogli` la scarta e non la
                # rivedra' nessuno.
                await su_righe([riga])
                return "continua"
            return "fine"

        tentativi = {"n": 0}

        async def apre_al_secondo_tentativo(nome):
            tentativi["n"] += 1
            return None if tentativi["n"] == 1 else "sfuggita_dal_dom"

        spia_apri: list = []
        await _monta(monkeypatch, camp, [riga], decidi_fine_lista_fake, spia_apri,
                     apri_riga_fake=apre_al_secondo_tentativo)

        await scrape_inbox_browser.run_inbox_browser_list(camp.id, db, camp)

        assert spia_apri == ["Sfuggita Dal Dom", "Sfuggita Dal Dom"], (
            f"la riga andava ritentata dopo il fallimento, spia={spia_apri}"
        )
        salvati = (await db.execute(
            select(Follower.username).where(Follower.campaign_id == camp.id)
        )).scalars().all()
        assert salvati == ["sfuggita_dal_dom"]


@pytest.mark.asyncio
async def test_una_riga_che_non_si_apre_mai_non_gira_in_tondo(monkeypatch):
    """Ramo negativo del ritentativo: rimettere in gioco una riga che non si
    risolve MAI significherebbe ripagarne la pausa a ogni giro per tutta la
    sessione. Dopo MAX_TENTATIVI_RIGA si lascia perdere fino alla prossima."""
    async with AsyncSessionLocal() as db:
        camp = Campaign(
            name="t-ritenta-tetto", status=CampaignStatus.listing,
            source_type="scrape", scrape_mode="dm_threads", inbox_engine="browser",
        )
        db.add(camp)
        await db.commit()
        await db.refresh(camp)

        riga = RigaVisibile(
            indice=0, nome="Mai Apribile", ultimo_nostro=None, non_letta=False,
            testo_grezzo="Mai Apribile\nciao",
        )

        giri = {"n": 0}

        async def decidi_fine_lista_fake(page_, falliti, lingua, su_righe):
            giri["n"] += 1
            await su_righe([riga])
            return "continua" if giri["n"] < 8 else "fine"

        async def non_apre_mai(nome):
            return None

        spia_apri: list = []
        await _monta(monkeypatch, camp, [riga], decidi_fine_lista_fake, spia_apri,
                     apri_riga_fake=non_apre_mai)

        await scrape_inbox_browser.run_inbox_browser_list(camp.id, db, camp)

        assert len(spia_apri) == scrape_inbox_browser.MAX_TENTATIVI_RIGA, (
            f"attesi {scrape_inbox_browser.MAX_TENTATIVI_RIGA} tentativi, spia={spia_apri}"
        )
