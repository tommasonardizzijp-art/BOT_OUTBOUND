"""Test dei difetti chiusi da M5.1 (review 07/08).

Regola di questo file: **orologio finto, configurazione vera**. I test
esistenti facevano il contrario (config azzerata con
`wa_resync_quarantine_min=0`, tempo finto a `browser_avviato_da_s=9999`) ed e'
esattamente per questo che il blocco della quarantena non e' mai emerso.
"""
import pytest

from tests.factories_wa import (make_campaign, make_campaign_contact,
                                make_contact, make_number, make_tenant)
from tests.helpers_wa_tempo import orologio_virtuale


async def scenario_pronto(db) -> dict:
    """Tenant + numero active (daily_cap 20, warmup_day 1) + campagna draft con
    step 0 + un contatto queued con appuntamento nel passato. E' il minimo che
    `wa_campaign_service.avvia()` accetta.

    Composto con le factory REALI di tests/factories_wa.py (patrimonio comune
    M2/M3, contratto sez. 5.1): non se ne aggiungono di nuove li' dentro.
    """
    tenant = await make_tenant(db)
    numero = await make_number(db, tenant)
    campagna, step = await make_campaign(db, tenant, numero)
    contatto = await make_contact(db, tenant)
    cc = await make_campaign_contact(db, campagna, contatto)
    await db.commit()
    return {"tenant": tenant, "number": numero, "campaign": campagna,
            "step": step, "contact": contatto, "cc": cc}


# ---------------------------------------------------------------------------
# T1 — la quarantena si aspetta, non fallisce
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_t1_quarantena_si_aspetta_non_fallisce(monkeypatch):
    """Con la config VERA (15 min), la mini-sessione aspetta la quarantena
    prima del primo claim invece di bruciare tre contatti e armare FM2."""
    from app.config import settings
    from app.workers import wa_worker

    assert settings.wa_resync_quarantine_min == 15, (
        "questo test vale contro la config vera: se il default cambia, "
        "aggiornare l'atteso, non azzerare la config")

    orologio = orologio_virtuale(wa_worker, monkeypatch)

    async def _mai_fermo():
        return False
    monkeypatch.setattr(wa_worker.bot_state_service, "is_wa_halted", _mai_fermo)

    async def _renew_ok(number_id, token, **kw):
        return True
    monkeypatch.setattr(wa_worker.wa_profile_lock, "renew", _renew_ok)

    completata = await wa_worker._attendi_quarantena_risync(
        "num-1", "tok-1", browser_t0=0.0)

    assert completata is True
    assert orologio["t"] >= settings.wa_resync_quarantine_min * 60, (
        "l'attesa deve coprire l'intera quarantena")


@pytest.mark.asyncio
async def test_t1_quarantena_interrotta_dal_kill_switch(monkeypatch):
    """Il kill-switch premuto durante l'attesa la interrompe: non si sta
    quindici minuti fermi ignorando uno stop."""
    from app.workers import wa_worker

    orologio_virtuale(wa_worker, monkeypatch)

    chiamate = {"n": 0}

    async def _fermo_al_secondo_giro():
        chiamate["n"] += 1
        return chiamate["n"] >= 2
    monkeypatch.setattr(wa_worker.bot_state_service, "is_wa_halted",
                        _fermo_al_secondo_giro)

    async def _renew_ok(number_id, token, **kw):
        return True
    monkeypatch.setattr(wa_worker.wa_profile_lock, "renew", _renew_ok)

    completata = await wa_worker._attendi_quarantena_risync(
        "num-1", "tok-1", browser_t0=0.0)

    assert completata is False


def test_t1_quarantena_non_arma_fm2():
    """`quarantena_risync` e' un limite nostro dichiarato, non un DOM rotto:
    non deve contare verso l'escalation che ferma il numero e manda un alert
    che dice 'probabile DOM cambiato'."""
    from app.workers import wa_worker

    assert "quarantena_risync" in wa_worker.MOTIVI_NON_FM2
    # I guasti veri restano guasti.
    assert "casella-ricerca-non-trovata" not in wa_worker.MOTIVI_NON_FM2


# ---------------------------------------------------------------------------
# T2 — avviare una campagna accoda il worker
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_t2_avviare_una_campagna_accoda_il_worker(db_session, monkeypatch):
    """Il difetto piu' silenzioso della review: start scriveva running e non
    accodava niente. La campagna restava 'In corso' senza nessun worker."""
    from app.services import wa_campaign_service as svc

    accodate = []

    async def _finta_enqueue(campaign_id: str) -> int:
        accodate.append(campaign_id)
        return 1

    monkeypatch.setattr("app.workers.wa_worker.enqueue_wa_workers", _finta_enqueue)

    ctx = await scenario_pronto(db_session)
    await svc.avvia(db_session, ctx["campaign"].id)

    assert accodate == [ctx["campaign"].id], (
        "avvia() deve accodare il worker: senza, la campagna e' running e "
        "nessuno invia")


@pytest.mark.asyncio
async def test_t2_redis_giu_non_annulla_l_avvio(db_session, monkeypatch):
    """Se l'accodamento fallisce, la campagna resta avviata: lo stato e' gia'
    committato e il supervisore riaccodera'. Perdere l'avvio sarebbe peggio."""
    from app.models.wa import WaCampaignStatus
    from app.services import wa_campaign_service as svc

    async def _enqueue_rotta(campaign_id: str) -> int:
        raise ConnectionError("redis irraggiungibile")

    monkeypatch.setattr("app.workers.wa_worker.enqueue_wa_workers", _enqueue_rotta)

    ctx = await scenario_pronto(db_session)
    campagna = await svc.avvia(db_session, ctx["campaign"].id)

    assert campagna.status == WaCampaignStatus.running
