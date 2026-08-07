"""Test dei difetti chiusi da M5.1 (review 07/08).

Regola di questo file: **orologio finto, configurazione vera**. I test
esistenti facevano il contrario (config azzerata con
`wa_resync_quarantine_min=0`, tempo finto a `browser_avviato_da_s=9999`) ed e'
esattamente per questo che il blocco della quarantena non e' mai emerso.
"""
from datetime import datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.database import get_db
from app.main import app
from app.models.user import User
from app.utils.auth_deps import get_current_user
from tests.factories_wa import (make_campaign, make_campaign_contact,
                                make_contact, make_number, make_tenant)
from tests.helpers_wa_tempo import orologio_virtuale


def _admin_utente() -> User:
    return User(id="00000000-0000-0000-0000-00000000005a",
                email="admin-wa-m51@test.local", password_hash="x",
                role="admin", is_active=True, created_at=datetime.utcnow())


@pytest_asyncio.fixture
async def client(db_session):
    """Stesso pattern di test_wa_api_campaign_lifecycle.py: override REALI di
    get_db e get_current_user. Senza, ogni richiesta risolve in 401 e il test
    passerebbe per costruzione senza esercitare la logica vera."""
    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _admin_utente
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


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


# ---------------------------------------------------------------------------
# T3 — da 'error' si esce
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_t3_recover_porta_da_error_a_paused(client, db_session):
    """Dopo FM2 la campagna finiva in error e non c'era piu' modo di
    riprenderla: l'unico verbo rimasto era 'stop'. Ora si recupera, ma con un
    atto esplicito e un motivo, e si passa da paused -- il resume rivalida."""
    from app.models.wa import WaCampaignStatus

    ctx = await scenario_pronto(db_session)
    ctx["campaign"].status = WaCampaignStatus.error
    await db_session.commit()

    r = await client.post(f"/api/wa/campaigns/{ctx['campaign'].id}/recover",
                          json={"motivo": "selettore risistemato, DOM verificato"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "paused"

    await db_session.refresh(ctx["campaign"])
    assert ctx["campaign"].status == WaCampaignStatus.paused


@pytest.mark.asyncio
async def test_t3_recover_rifiuta_una_campagna_non_in_error(client, db_session):
    ctx = await scenario_pronto(db_session)      # draft
    r = await client.post(f"/api/wa/campaigns/{ctx['campaign'].id}/recover",
                          json={"motivo": "x"})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_t3_recover_senza_motivo_e_rifiutato(client, db_session):
    """Uno stato lasciato da un incidente si toglie a mano, lasciando traccia."""
    from app.models.wa import WaCampaignStatus

    ctx = await scenario_pronto(db_session)
    ctx["campaign"].status = WaCampaignStatus.error
    await db_session.commit()

    r = await client.post(f"/api/wa/campaigns/{ctx['campaign'].id}/recover",
                          json={"motivo": "   "})
    assert r.status_code == 422

    await db_session.refresh(ctx["campaign"])
    assert ctx["campaign"].status == WaCampaignStatus.error


@pytest.mark.asyncio
async def test_t3_recover_non_riavvia_da_solo(client, db_session, monkeypatch):
    """Il recupero NON fa ripartire gli invii: porta a paused e basta. Se
    riportasse a running salterebbe le validazioni di avvio (numero attivo,
    nessun'altra campagna sullo stesso numero) che vivono in `avvia`."""
    from app.models.wa import WaCampaignStatus

    accodate = []

    async def _finta_enqueue(campaign_id: str) -> int:
        accodate.append(campaign_id)
        return 1
    monkeypatch.setattr("app.workers.wa_worker.enqueue_wa_workers", _finta_enqueue)

    ctx = await scenario_pronto(db_session)
    ctx["campaign"].status = WaCampaignStatus.error
    await db_session.commit()

    await client.post(f"/api/wa/campaigns/{ctx['campaign'].id}/recover",
                      json={"motivo": "verificato"})

    assert accodate == [], "il recupero non deve far ripartire nulla da solo"


# ---------------------------------------------------------------------------
# T4 — l'health-check non annulla il cooldown
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_t4_cooldown_non_diventa_active_per_una_lettura_del_dom(db_session):
    """FM2 mette il numero in cooldown per 4 ore. L'health-check gira ogni 30
    minuti, vedeva la sessione viva e lo rimetteva active: lo stop durava
    mezz'ora invece di quattro ore. E' la porta gemella di quella gia' chiusa
    dentro _ferma_numero_per_guasto."""
    from app.models.wa import WaNumberStatus
    from app.services import wa_session

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant, status=WaNumberStatus.cooldown)
    await db_session.commit()

    await wa_session._persist_status(numero.id, WaNumberStatus.active)
    await db_session.refresh(numero)

    assert numero.status == WaNumberStatus.cooldown


@pytest.mark.asyncio
async def test_t4_cooldown_puo_peggiorare(db_session):
    """Un numero in cooldown la cui sessione e' caduta davvero DEVE poter
    diventare disconnected: e' informazione vera, e serve al cron per mettere
    in pausa le campagne. Si blocca solo la PROMOZIONE."""
    from app.models.wa import WaNumberStatus
    from app.services import wa_session

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant, status=WaNumberStatus.cooldown)
    await db_session.commit()

    await wa_session._persist_status(numero.id, WaNumberStatus.disconnected)
    await db_session.refresh(numero)

    assert numero.status == WaNumberStatus.disconnected


@pytest.mark.asyncio
async def test_t4_la_scadenza_del_timer_toglie_ancora_il_cooldown(db_session, monkeypatch):
    """Il rimedio legittimo resta: release_expired_wa_cooldowns scrive con una
    UPDATE diretta e non passa da _persist_status, quindi la guardia sopra non
    lo tocca. Se lo toccasse, un numero resterebbe in cooldown per sempre."""
    from app.models.wa import WaNumberStatus
    from app.services import wa_number_manager

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant, status=WaNumberStatus.cooldown)
    await db_session.commit()

    async def _timer_scaduto(number_id: str) -> bool:
        return False
    monkeypatch.setattr(wa_number_manager, "is_wa_cooldown_active", _timer_scaduto)

    rilasciati = await wa_number_manager.release_expired_wa_cooldowns()
    await db_session.refresh(numero)

    assert numero.id in rilasciati
    assert numero.status == WaNumberStatus.active
