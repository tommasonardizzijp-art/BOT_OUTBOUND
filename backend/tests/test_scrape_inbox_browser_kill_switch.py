# backend/tests/test_scrape_inbox_browser_kill_switch.py
"""I2 Important: il kill-switch va controllato per RIGA, non solo per lotto.

Prima del fix, is_halted/stato-campagna/motore erano controllati solo in cima
al `while True:`, prima di leggere un lotto di righe (fino a 30). Con
`campiona_pausa` che estrae fino a 2-5 minuti di stacco fra una riga e la
successiva, il browser restava vivo diversi minuti dopo che l'utente aveva
premuto stop — la spec chiede uno stop immediato.
"""
from types import SimpleNamespace

import pytest

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
        return None


class _FakeBrowserSession:
    """`BrowserSession` e' importata localmente dentro run_inbox_browser_list:
    va patchata alla fonte (app.browser.context_manager.BrowserSession)."""
    pagina_condivisa = None

    def __init__(self, account_id):
        self.account_id = account_id
        self.context = SimpleNamespace(pages=[_FakeBrowserSession.pagina_condivisa])

    async def open(self):
        return None

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_kill_switch_ferma_a_meta_lotto_non_solo_a_fine_lotto(monkeypatch):
    """Con is_halted che diventa True a META' di un lotto di 5 righe simulate
    (tutte gia' riconosciute, cosi' il test isola il controllo kill-switch dal
    resto della pipeline), il ciclo deve uscire SENZA processare le righe
    rimanenti — non solo al giro successivo del while."""
    async with AsyncSessionLocal() as db:
        camp = Campaign(
            name="t-kill-switch", status=CampaignStatus.listing,
            source_type="scrape", scrape_mode="dm_threads", inbox_engine="browser",
        )
        db.add(camp)
        await db.flush()
        # Tutte gia' note: decide_se_aprire ritorna sempre False, cosi' non
        # serve simulare anche apri_riga/salva_contatto per isolare il fix.
        for i in range(5):
            db.add(Follower(
                campaign_id=camp.id, ig_user_id=-(9000000 + i), username=f"noto{i}",
                full_name=f"Noto {i}", status=FollowerStatus.pending,
            ))
        await db.commit()
        await db.refresh(camp)

        page = _FakePage()
        _FakeBrowserSession.pagina_condivisa = page
        monkeypatch.setattr("app.browser.context_manager.BrowserSession", _FakeBrowserSession)
        monkeypatch.setattr("app.utils.events.emit", lambda *a, **k: None)

        account = SimpleNamespace(id="acc-1", username="mio_account")

        async def fake_single_account(db_, campaign_id):
            return account
        monkeypatch.setattr(scrape_inbox_browser, "_single_inbox_account", fake_single_account)

        righe = [
            RigaVisibile(indice=i, nome=f"Noto {i}", ultimo_nostro=None, non_letta=False, testo_grezzo=f"Noto {i}")
            for i in range(5)
        ]

        async def fake_leggi_righe(page_, lingua):
            return righe
        monkeypatch.setattr(scrape_inbox_browser, "leggi_righe_visibili", fake_leggi_righe)

        chiamate_halted = {"n": 0}

        async def fake_is_halted(db_):
            chiamate_halted["n"] += 1
            # 1a chiamata: controllo in cima al while (prima del lotto) -> False.
            # 2a e 3a: prime due righe del for -> False. 4a (terza riga) -> True.
            return chiamate_halted["n"] >= 4
        monkeypatch.setattr(scrape_inbox_browser, "is_halted", fake_is_halted)

        righe_processate = []

        def fake_campiona_pausa(zona):
            righe_processate.append(zona)
            return 0
        monkeypatch.setattr(scrape_inbox_browser, "campiona_pausa", fake_campiona_pausa)

        risultato = await scrape_inbox_browser.run_inbox_browser_list(camp.id, db, camp)

        assert risultato is None
        assert len(righe_processate) == 2, (
            f"doveva fermarsi dopo 2 righe (kill-switch a meta' lotto), non {len(righe_processate)}"
        )
        await db.refresh(camp)
        assert camp.status == CampaignStatus.paused
