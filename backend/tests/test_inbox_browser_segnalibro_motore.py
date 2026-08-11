"""Modalita' segnalibro dentro il ciclo reale di run_inbox_browser_list.

Task 8 del piano velocita' inbox browser. Le funzioni pure del segnalibro
(Task 7) sono gia' testate in isolamento: qui si verifica che il ciclo le usi
correttamente, con lo stesso rigore del Task 4a (giro_completo) — l'integrazione
nel motore reale e' dove i bug si nascondono, non nelle funzioni pure.
"""
from types import SimpleNamespace

import pytest

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus
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


async def _monta(monkeypatch, righe_iniziali, decidi_fine_lista_fake,
                  salta_lavorate, spia_apri=None, spia_lancia=None):
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
        chiave = "".join(c for c in (nome or "") if c.isalnum()) or "anon"
        return f"user_{chiave}"
    monkeypatch.setattr(scrape_inbox_browser, "apri_riga", fake_apri_riga)

    async def fake_lancia(page_):
        if spia_lancia is not None:
            spia_lancia.append(True)
        from app.services.inbox_browser.pagina import StatoScorrimento
        return StatoScorrimento(altezza=1000, al_fondo=False)
    monkeypatch.setattr(scrape_inbox_browser, "lancia", fake_lancia)

    return page


def _campagna(**kw):
    return Campaign(
        name=kw.pop("name", "t-segnalibro-motore"), status=CampaignStatus.listing,
        source_type="scrape", scrape_mode="dm_threads", inbox_engine="browser",
        **kw,
    )


@pytest.mark.asyncio
async def test_riga_piu_recente_della_soglia_si_salta_senza_aprire(monkeypatch):
    """Con la modalita' accesa e un cursore che segna 'arrivato a 120h fa', una
    riga di 20h fa (sopra la soglia, zona gia' lavorata) non deve mai passare
    per apri_riga."""
    from datetime import datetime, timedelta

    async with AsyncSessionLocal() as db:
        camp = _campagna(inbox_cursor_at=datetime.utcnow() - timedelta(hours=120))
        camp.inbox_salta_lavorate = True
        db.add(camp)
        await db.commit()
        await db.refresh(camp)
        camp.inbox_salta_lavorate = True

        righe = [RigaVisibile(indice=0, nome="Recente", ultimo_nostro=None,
                               non_letta=False, testo_grezzo="Recente\nciao\n20 h")]

        async def decidi_fine_lista_fake(page_, falliti, lingua, su_righe):
            return "fine"

        spia_apri: list = []
        await _monta(monkeypatch, righe, decidi_fine_lista_fake, True, spia_apri)

        await scrape_inbox_browser.run_inbox_browser_list(camp.id, db, camp)

        assert spia_apri == [], f"riga in zona segnalibro aperta: {spia_apri}"


@pytest.mark.asyncio
async def test_riga_piu_vecchia_della_soglia_si_apre_comunque(monkeypatch):
    """Una riga PIU' vecchia della soglia (7 giorni fa, sotto le 120h del
    cursore) e' zona nuova: va aperta normalmente anche a modalita' accesa."""
    from datetime import datetime, timedelta

    async with AsyncSessionLocal() as db:
        camp = _campagna(inbox_cursor_at=datetime.utcnow() - timedelta(hours=120))
        camp.inbox_salta_lavorate = True
        db.add(camp)
        await db.commit()
        await db.refresh(camp)
        camp.inbox_salta_lavorate = True

        righe = [RigaVisibile(indice=0, nome="Vecchia Chat", ultimo_nostro=None,
                               non_letta=False, testo_grezzo="Vecchia Chat\nciao\n7 g")]

        async def decidi_fine_lista_fake(page_, falliti, lingua, su_righe):
            return "fine"

        spia_apri: list = []
        await _monta(monkeypatch, righe, decidi_fine_lista_fake, True, spia_apri)

        await scrape_inbox_browser.run_inbox_browser_list(camp.id, db, camp)

        assert spia_apri == ["Vecchia Chat"]


@pytest.mark.asyncio
async def test_modalita_spenta_apre_tutto_nonostante_il_cursore(monkeypatch):
    """Un cursore presente ma la spunta spenta: la sessione legge tutto, e'
    quella che recupera chi e' risalito."""
    from datetime import datetime, timedelta

    async with AsyncSessionLocal() as db:
        camp = _campagna(inbox_cursor_at=datetime.utcnow() - timedelta(hours=120))
        db.add(camp)
        await db.commit()
        await db.refresh(camp)
        # inbox_salta_lavorate MAI impostato: default spento.

        righe = [RigaVisibile(indice=0, nome="Recente", ultimo_nostro=None,
                               non_letta=False, testo_grezzo="Recente\nciao\n20 h")]

        async def decidi_fine_lista_fake(page_, falliti, lingua, su_righe):
            return "fine"

        spia_apri: list = []
        await _monta(monkeypatch, righe, decidi_fine_lista_fake, False, spia_apri)

        await scrape_inbox_browser.run_inbox_browser_list(camp.id, db, camp)

        assert spia_apri == ["Recente"]


@pytest.mark.asyncio
async def test_giro_di_sole_righe_saltate_lancia_invece_di_scrollare_normale(monkeypatch):
    """Se in un giro TUTTE le righe incontrate sono nella zona da saltare, il
    ciclo deve usare lancia() (flick lungo, nessuna lettura) per attraversarla
    in fretta, invece del normale decidi_fine_lista + scorri_leggendo."""
    from datetime import datetime, timedelta

    async with AsyncSessionLocal() as db:
        camp = _campagna(inbox_cursor_at=datetime.utcnow() - timedelta(hours=120))
        camp.inbox_salta_lavorate = True
        db.add(camp)
        await db.commit()
        await db.refresh(camp)
        camp.inbox_salta_lavorate = True

        righe_zona_saltata = [
            RigaVisibile(indice=i, nome=f"Recente {i}", ultimo_nostro=None,
                         non_letta=False, testo_grezzo=f"Recente {i}\nciao\n20 h")
            for i in range(3)
        ]

        chiamate_decidi = {"n": 0}

        async def decidi_fine_lista_fake(page_, falliti, lingua, su_righe):
            chiamate_decidi["n"] += 1
            return "fine"

        spia_apri: list = []
        spia_lancia: list = []
        await _monta(monkeypatch, righe_zona_saltata, decidi_fine_lista_fake,
                     True, spia_apri, spia_lancia)

        await scrape_inbox_browser.run_inbox_browser_list(camp.id, db, camp)

        assert spia_apri == []
        assert len(spia_lancia) >= 1, "lancia() non chiamata sul primo giro di sole righe saltate"
        # decidi_fine_lista PUO' essere chiamata dopo: il fake ritorna sempre
        # le stesse 3 righe, quindi al secondo giro la memoria di sessione
        # (Task 2) le ha gia' tutte viste e non c'e' piu' nulla di nuovo da
        # valutare (righe_incontrate torna a 0) — a quel punto e' corretto
        # ricadere su decidi_fine_lista per accertare la fine lista, non un
        # bug. Quello che conta e' che lancia() sia stata usata PRIMA, per il
        # giro che aveva davvero solo righe da saltare.
        assert chiamate_decidi["n"] <= 1


@pytest.mark.asyncio
async def test_cursore_si_aggiorna_dopo_un_contatto_salvato(monkeypatch):
    """Salvare un contatto in zona nuova (piu' vecchia della soglia) deve
    spostare il cursore in avanti (verso il passato), non lasciarlo fermo."""
    from datetime import datetime, timedelta

    async with AsyncSessionLocal() as db:
        cursore_iniziale = datetime.utcnow() - timedelta(hours=120)
        camp = _campagna(inbox_cursor_at=cursore_iniziale)
        camp.inbox_salta_lavorate = True
        db.add(camp)
        await db.commit()
        await db.refresh(camp)
        camp.inbox_salta_lavorate = True

        righe = [RigaVisibile(indice=0, nome="Molto Vecchia", ultimo_nostro=None,
                               non_letta=False, testo_grezzo="Molto Vecchia\nciao\n10 g")]

        async def decidi_fine_lista_fake(page_, falliti, lingua, su_righe):
            return "fine"

        await _monta(monkeypatch, righe, decidi_fine_lista_fake, True)

        await scrape_inbox_browser.run_inbox_browser_list(camp.id, db, camp)

        await db.refresh(camp)
        assert camp.inbox_cursor_at is not None
        assert camp.inbox_cursor_at < cursore_iniziale, (
            "il cursore doveva scendere (10g nel passato) dopo il contatto salvato"
        )


@pytest.mark.asyncio
async def test_cursore_si_aggiorna_anche_su_esito_aggiornato(monkeypatch):
    """Prova del nove del bug trovato in review: quando salva_contatto ritorna
    'aggiornato' (contatto gia' noto) invece di 'creato', il commit del
    cursore avveniva solo dentro il ramo 'creato'. Il prossimo
    `_deve_fermarsi()` (prima istruzione del giro successivo) fa
    `db.refresh(campaign)`, che scarta la modifica mai committata — il
    cursore restava fermo quasi sempre nella STESSA sessione, non solo al
    riavvio del processo."""
    from datetime import datetime, timedelta

    from app.models.follower import Follower, FollowerStatus

    async with AsyncSessionLocal() as db:
        cursore_iniziale = datetime.utcnow() - timedelta(hours=120)
        camp = _campagna(inbox_cursor_at=cursore_iniziale)
        camp.inbox_salta_lavorate = True
        db.add(camp)
        await db.flush()
        # Follower pre-esistente con lo STESSO username che fake_apri_riga
        # produrra' per "Molto Vecchia" (vedi _monta: f"user_{chiave}",
        # poi normalizza_username lo abbassa di caso): salva_contatto trovera'
        # questa riga e rispondera' 'aggiornato', non 'creato'. full_name
        # DIVERSO apposta: se fosse "Molto Vecchia" ArchivioNomi (costruito
        # dai full_name esistenti) la riconoscerebbe e decide_se_aprire non
        # aprirebbe mai la riga, invalidando il test (il cursore non si
        # muoverebbe per un motivo diverso da quello sotto esame).
        db.add(Follower(
            campaign_id=camp.id, ig_user_id=-1, username="user_moltovecchia",
            full_name="Nome Gia' Salvato Con Altro Nome", status=FollowerStatus.pending,
        ))
        await db.commit()
        await db.refresh(camp)
        camp.inbox_salta_lavorate = True

        righe = [RigaVisibile(indice=0, nome="Molto Vecchia", ultimo_nostro=None,
                               non_letta=False, testo_grezzo="Molto Vecchia\nciao\n10 g")]

        async def decidi_fine_lista_fake(page_, falliti, lingua, su_righe):
            return "fine"

        await _monta(monkeypatch, righe, decidi_fine_lista_fake, True)

        await scrape_inbox_browser.run_inbox_browser_list(camp.id, db, camp)

        await db.refresh(camp)
        assert camp.inbox_cursor_at is not None
        assert camp.inbox_cursor_at < cursore_iniziale, (
            "il cursore doveva scendere anche con esito 'aggiornato', "
            f"e' rimasto a {camp.inbox_cursor_at} (era {cursore_iniziale})"
        )


@pytest.mark.asyncio
async def test_soglia_che_copre_tutta_la_lista_non_gira_a_vuoto_per_sempre(monkeypatch):
    """Caso limite: il cursore e' cosi' profondo che OGNI riga incontrata va
    saltata. Senza guardia il motore lancerebbe all'infinito senza mai
    raggiungere decidi_fine_lista. Deve fermarsi entro un numero finito di
    lanci, con la campagna in uno stato pulito (non 'error')."""
    from datetime import datetime, timedelta

    async with AsyncSessionLocal() as db:
        camp = _campagna(inbox_cursor_at=datetime.utcnow() - timedelta(days=365 * 5))
        camp.inbox_salta_lavorate = True
        db.add(camp)
        await db.commit()
        await db.refresh(camp)
        camp.inbox_salta_lavorate = True

        # Zona INFINITA da saltare: ogni lettura mostra una riga NUOVA (mai
        # vista), sempre recentissima. Simula uno scroll che continua a
        # scoprire chat da saltare senza mai arrivare in fondo — il caso reale
        # che la guardia deve fermare, diverso da "le stesse righe di sempre"
        # (che raccogli() filtrerebbe gia' da sola via viste_in_sessione).
        contatore_letture = {"n": 0}
        page_fittizia = _FakeBrowserSession.pagina_condivisa

        async def decidi_fine_lista_fake(page_, falliti, lingua, su_righe):
            raise AssertionError("non deve mai raggiungere decidi_fine_lista in questo scenario")

        spia_lancia: list = []
        await _monta(monkeypatch, [], decidi_fine_lista_fake, True, None, spia_lancia)

        async def fake_leggi_righe_infinite(page_, lingua):
            contatore_letture["n"] += 1
            n = contatore_letture["n"]
            return [RigaVisibile(indice=0, nome=f"Recente {n}", ultimo_nostro=None,
                                  non_letta=False, testo_grezzo=f"Recente {n}\nciao\n1 h")]
        monkeypatch.setattr(scrape_inbox_browser, "leggi_righe_visibili", fake_leggi_righe_infinite)

        risultato = await scrape_inbox_browser.run_inbox_browser_list(camp.id, db, camp)

        assert risultato is None
        assert len(spia_lancia) < 1000, "guardia sui lanci a vuoto non ha fermato il ciclo"
        await db.refresh(camp)
        assert camp.status != CampaignStatus.error
