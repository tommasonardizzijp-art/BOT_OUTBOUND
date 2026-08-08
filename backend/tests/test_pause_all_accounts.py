"""L'attivita' browser in pausa deve coprire TUTTI gli account scraping della
campagna (non solo l'ultimo usato), in parallelo ma scaglionati."""
import asyncio

import app.services.browser_bio as bb


def test_all_accounts_get_a_session(monkeypatch):
    called = []

    async def fake_activity(campaign_id, account_id, username=None):
        called.append(account_id)
        return 1

    async def fake_accounts(campaign_id):
        return [("a1", "u1"), ("a2", "u2"), ("a3", "u3")]

    async def no_sleep(_):
        return None

    monkeypatch.setattr(bb.settings, "bio_browser_batch_enabled", True)
    monkeypatch.setattr(bb, "run_pause_browser_activity", fake_activity)
    monkeypatch.setattr(bb, "_scraping_accounts_of_campaign", fake_accounts)
    monkeypatch.setattr(bb.asyncio, "sleep", no_sleep)

    asyncio.run(bb.run_pause_browser_all_accounts("camp1"))
    assert set(called) == {"a1", "a2", "a3"}  # tutti coperti


def test_noop_when_all_flags_off(monkeypatch):
    called = []

    async def fake_activity(campaign_id, account_id, username=None):
        called.append(account_id)
        return 1

    async def fake_accounts(campaign_id):
        return [("a1", "u1")]

    monkeypatch.setattr(bb.settings, "warmup_browse_enabled", False)
    monkeypatch.setattr(bb.settings, "bio_browser_batch_enabled", False)
    monkeypatch.setattr(bb, "run_pause_browser_activity", fake_activity)
    monkeypatch.setattr(bb, "_scraping_accounts_of_campaign", fake_accounts)

    spent = asyncio.run(bb.run_pause_browser_all_accounts("camp1"))
    assert spent == 0
    assert called == []  # niente sessioni se tutto OFF


def test_cap_is_global_across_concurrent_calls(monkeypatch):
    """max_concurrent_browsers deve essere un tetto GLOBALE AL PROCESSO, non
    per-chiamata: se due campagne entrano in pausa bio nello stesso momento, il
    numero di sessioni browser aperte insieme resta <= cap, non 2x cap (una
    campagna non deve poter raddoppiare il tetto degli altri).

    Prova del nove: se il semaforo torna locale a run_pause_browser_all_accounts
    (come prima del fix, creato dentro la funzione a ogni chiamata) questo test
    diventa ROSSO -- ogni campagna apre fino a `cap` sessioni proprie, il picco
    osservato sale a 2x cap."""
    cap = 2
    in_flight = 0
    peak = 0

    async def fake_activity(campaign_id, account_id, username=None):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        # sleep REALE (non patchato) solo per lasciare interlacciare le due
        # campagne concorrenti sullo stesso loop -- niente a che fare col cap.
        await asyncio.sleep(0.02)
        in_flight -= 1
        return 1

    async def accounts_of(campaign_id):
        # 4 account per campagna x 2 campagne = 8 sessioni totali richieste,
        # ben sopra il cap: se il tetto fosse per-chiamata si vedrebbe subito.
        return [(f"{campaign_id}-{i}", f"u{i}") for i in range(4)]

    monkeypatch.setattr(bb.settings, "bio_browser_batch_enabled", True)
    monkeypatch.setattr(bb.settings, "max_concurrent_browsers", cap)
    monkeypatch.setattr(bb, "run_pause_browser_activity", fake_activity)
    monkeypatch.setattr(bb, "_scraping_accounts_of_campaign", accounts_of)
    # Stagger azzerato (no vero delay nel test) ma con un vero yield: niente
    # asyncio.sleep patchato a no-op, cosi' le due campagne restano davvero
    # concorrenti sullo stesso event loop invece di girare l'una dopo l'altra.
    monkeypatch.setattr(bb.random, "uniform", lambda lo, hi: 0.0)

    async def run_both():
        await asyncio.gather(
            bb.run_pause_browser_all_accounts("campA"),
            bb.run_pause_browser_all_accounts("campB"),
        )

    asyncio.run(run_both())

    assert peak <= cap, f"picco {peak} supera il tetto globale {cap} — cap non e' condiviso tra chiamate"
    assert peak >= cap, "concorrenza mai raggiunta: il test non contende il tetto, non proverebbe nulla"
