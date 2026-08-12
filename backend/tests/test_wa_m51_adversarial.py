"""Adversarial mirata sulla superficie di M5.1.

Criterio di PASS **invertito** (protocollo `sviluppo-modulo` Fase 4): passa se
il sistema **si difende** -- errore chiaro, nessuna scrittura sporca,
invariante intatta. Un 500, un errore DB grezzo, una scrittura parziale o
un'invariante violata sono FAIL anche se "sembrava funzionare".

Livelli mescolati: chiamata diretta al servizio per race e config ostile,
HTTP vero per i payload malformati. Un adversarial che passa da un solo
livello non e' un adversarial.

Perimetro: solo cio' che M5.1 ha toccato. Il resto del canale ha gia' la sua
batteria (test_wa_m4_adversarial.py, e le liste QA di M5 nel second-brain).
"""
import asyncio
from datetime import datetime, timedelta

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


def _admin() -> User:
    return User(id="00000000-0000-0000-0000-00000000005b",
                email="admin-wa-m51-adv@test.local", password_hash="x",
                role="admin", is_active=True, created_at=datetime.utcnow())


@pytest_asyncio.fixture
async def client(db_session):
    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _admin
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _scenario(db) -> dict:
    tenant = await make_tenant(db)
    numero = await make_number(db, tenant)
    campagna, step = await make_campaign(db, tenant, numero)
    contatto = await make_contact(db, tenant)
    cc = await make_campaign_contact(db, campagna, contatto)
    await db.commit()
    return {"tenant": tenant, "number": numero, "campaign": campagna,
            "step": step, "contact": contatto, "cc": cc}


@pytest.fixture
def enqueue_spia(monkeypatch):
    accodate: list[str] = []

    async def _finta(campaign_id: str, **kw) -> int:
        accodate.append(campaign_id)
        return 1

    monkeypatch.setattr("app.workers.wa_worker.enqueue_wa_workers", _finta)
    return accodate


# ===========================================================================
# A — Configurazione ostile della quarantena
# ===========================================================================

@pytest.mark.asyncio
async def test_a1_quarantena_zero_non_aspetta(monkeypatch):
    """A quarantena disattivata non si aspetta: e' una scelta legittima di chi
    configura, non un caso da bloccare."""
    from app.config import settings
    from app.workers import wa_worker

    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 0)
    orologio = orologio_virtuale(wa_worker, monkeypatch)

    assert await wa_worker._attendi_quarantena_risync("n", "t", 0.0) is None
    assert orologio["t"] == 0.0


@pytest.mark.asyncio
async def test_a2_quarantena_negativa_non_blocca_ne_cicla(monkeypatch):
    """Un valore negativo in .env non deve produrre un'attesa infinita ne'
    un'eccezione: si comporta come 'nessuna quarantena'."""
    from app.config import settings
    from app.workers import wa_worker

    monkeypatch.setattr(settings, "wa_resync_quarantine_min", -30)
    orologio = orologio_virtuale(wa_worker, monkeypatch)

    assert await wa_worker._attendi_quarantena_risync("n", "t", 0.0) is None
    assert orologio["t"] == 0.0


@pytest.mark.asyncio
async def test_a3_quarantena_piu_lunga_della_sessione_non_apre_il_browser(monkeypatch):
    """Il caso che rompe tutto in silenzio: una quarantena piu' lunga del cap
    wall-clock terrebbe il profilo aperto oltre il TTL del lucchetto senza
    mandare un solo messaggio. Deve uscire subito con un motivo dedicato."""
    from app.config import settings
    from app.workers import wa_worker

    monkeypatch.setattr(settings, "wa_profile_lock_ttl_min", 90)     # cap 85 min
    monkeypatch.setattr(settings, "wa_resync_quarantine_min", 120)
    orologio = orologio_virtuale(wa_worker, monkeypatch)

    motivo = await wa_worker._attendi_quarantena_risync("n", "t", 0.0)

    assert motivo == "quarantena_oltre_cap_sessione"
    assert orologio["t"] == 0.0, "non deve aver aspettato nemmeno un secondo"


@pytest.mark.asyncio
async def test_a4_quarantena_gia_scaduta_non_riaspetta(monkeypatch):
    """browser_t0 vecchio (sessione ripresa): l'attesa e' gia' stata fatta,
    non si ricomincia da capo."""
    from app.config import settings
    from app.workers import wa_worker

    orologio = orologio_virtuale(wa_worker, monkeypatch)
    orologio["t"] = 10_000.0

    motivo = await wa_worker._attendi_quarantena_risync("n", "t", browser_t0=0.0)

    assert motivo is None
    assert orologio["t"] == 10_000.0
    assert settings.wa_resync_quarantine_min == 2    # config vera (08/08), non toccata


@pytest.mark.asyncio
async def test_a5_una_config_rotta_non_rischedula_all_infinito(db_session, monkeypatch):
    """wa_send_task non deve riprovare ogni venti minuti con una config che per
    costruzione non puo' funzionare: si ferma e si fa notare."""
    from arq.worker import Retry
    from app.workers import wa_worker

    async def _finta_sessione(number_id):
        return {"inviati": 0, "falliti": 0, "saltati": 0,
                "motivo": "quarantena_oltre_cap_sessione"}

    monkeypatch.setattr(wa_worker, "esegui_mini_sessione", _finta_sessione)

    try:
        await wa_worker.wa_send_task({}, "num-x")
    except Retry:
        pytest.fail("una configurazione incoerente non deve essere rischedulata")


@pytest.mark.asyncio
async def test_a6_il_cap_di_sessione_sta_sotto_al_timeout_di_arq(monkeypatch):
    """Il cap wall-clock deve stare sotto ANCHE al job_timeout di ARQ, non solo
    sotto il TTL del lucchetto. Se ARQ uccide la coroutine a meta', il contatto
    claimato resta lockato per venti minuti e il browser muore male."""
    from app.config import settings
    from app.workers import wa_worker
    from app.workers.task_queue import WorkerSettings

    assert wa_worker.ARQ_JOB_TIMEOUT_S == WorkerSettings.job_timeout, (
        "la costante e' una copia: se job_timeout cambia in task_queue, va "
        "cambiata anche in wa_worker (l'import diretto creerebbe un ciclo)")

    monkeypatch.setattr(settings, "wa_profile_lock_ttl_min", 90)
    assert wa_worker._limite_sessione_s() < WorkerSettings.job_timeout

    # E con un TTL corto continua a comandare il lucchetto, che e' il vincolo
    # piu' stretto in quel caso.
    monkeypatch.setattr(settings, "wa_profile_lock_ttl_min", 30)
    assert wa_worker._limite_sessione_s() == (30 - 5) * 60


@pytest.mark.asyncio
async def test_a7_lucchetto_perso_durante_l_attesa_annulla_la_sessione(monkeypatch):
    """Quindici minuti di attesa sono la finestra in cui e' piu' probabile
    perdere il lucchetto e meno probabile che qualcuno guardi. Proseguire
    significherebbe un secondo Chromium sullo stesso profilo."""
    from app.workers import wa_worker

    orologio_virtuale(wa_worker, monkeypatch)

    async def _mai_fermo():
        return False
    monkeypatch.setattr(wa_worker.bot_state_service, "is_wa_halted", _mai_fermo)

    async def _renew_perso(number_id, token, **kw):
        return False
    monkeypatch.setattr(wa_worker.wa_profile_lock, "renew", _renew_perso)

    motivo = await wa_worker._attendi_quarantena_risync("n", "tok", 0.0)
    assert motivo == "profilo_occupato"


# ===========================================================================
# A-bis — il pre-check che evita di aprire il browser per niente
# ===========================================================================

@pytest.mark.asyncio
async def test_a8_niente_da_fare_non_apre_il_browser(db_session, monkeypatch):
    """Il difetto piu' costoso introdotto dall'attesa: senza un pre-check, un
    numero col cap esaurito apriva WhatsApp Web, teneva il lucchetto per un
    quarto d'ora senza mandare niente, chiudeva, e ricominciava dopo il break --
    tutta la notte."""
    from app.config import settings
    from app.models.wa import WaCampaignStatus
    from app.services import wa_number_manager
    from app.workers import wa_worker

    monkeypatch.setattr(settings, "wa_send_enabled", True)

    ctx = await _scenario(db_session)
    ctx["campaign"].status = WaCampaignStatus.running
    await db_session.commit()

    async def _senza_budget(db, number, campaign):
        return False
    monkeypatch.setattr(wa_number_manager, "has_wa_send_budget", _senza_budget)
    monkeypatch.setattr(wa_worker, "_ora_locale_corrente", lambda: 11)

    def _boom(*a, **kw):
        raise AssertionError("il browser non doveva nemmeno aprirsi")
    monkeypatch.setattr(wa_worker, "_open_wa_browser", _boom)

    esito = await wa_worker.esegui_mini_sessione(ctx["number"].id)
    assert esito["motivo"] == "cap_esaurito"


@pytest.mark.asyncio
async def test_a9_fuori_finestra_non_apre_il_browser(db_session, monkeypatch):
    from app.config import settings
    from app.models.wa import WaCampaignStatus
    from app.workers import wa_worker

    monkeypatch.setattr(settings, "wa_send_enabled", True)

    ctx = await _scenario(db_session)
    ctx["campaign"].status = WaCampaignStatus.running
    await db_session.commit()

    monkeypatch.setattr(wa_worker, "_ora_locale_corrente", lambda: 3)   # notte

    def _boom(*a, **kw):
        raise AssertionError("il browser non doveva nemmeno aprirsi")
    monkeypatch.setattr(wa_worker, "_open_wa_browser", _boom)

    esito = await wa_worker.esegui_mini_sessione(ctx["number"].id)
    assert esito["motivo"] == "fuori_finestra"


@pytest.mark.asyncio
async def test_a10_il_precheck_non_locka_niente(db_session, monkeypatch):
    """Un pre-check che claimasse una riga la terrebbe ferma per tutta la
    quarantena. Deve essere in sola lettura."""
    from app.models.wa import WaCampaignStatus
    from app.workers import wa_worker

    ctx = await _scenario(db_session)
    ctx["campaign"].status = WaCampaignStatus.running
    await db_session.commit()

    monkeypatch.setattr(wa_worker, "_ora_locale_corrente", lambda: 11)
    assert await wa_worker._niente_da_fare_prima_del_browser(ctx["number"].id) is None

    await db_session.refresh(ctx["cc"])
    assert ctx["cc"].locked_by is None and ctx["cc"].locked_at is None


# ===========================================================================
# B — Supervisore delle campagne running
# ===========================================================================

@pytest.mark.asyncio
async def test_b1_supervisore_senza_campagne_non_esplode(db_session, enqueue_spia):
    from app.workers import cron_worker

    esito = await cron_worker.wa_campaign_supervisor({})
    assert isinstance(esito["controllate"], int)
    assert isinstance(esito["riaccodate"], int)


@pytest.mark.asyncio
async def test_b2_un_enqueue_rotto_non_ferma_le_altre_campagne(db_session, monkeypatch):
    """Un guasto su una campagna non deve abortire il giro: le altre devono
    essere comunque riaccodate."""
    from app.models.wa import WaCampaignStatus
    from app.workers import cron_worker

    a = await _scenario(db_session)
    b = await _scenario(db_session)
    for ctx in (a, b):
        ctx["campaign"].status = WaCampaignStatus.running
    await db_session.commit()

    visti: list[str] = []

    async def _enqueue_capriccioso(campaign_id: str, **kw) -> int:
        visti.append(campaign_id)
        if campaign_id == a["campaign"].id:
            raise ConnectionError("redis giu' proprio su questa")
        return 1

    monkeypatch.setattr("app.workers.wa_worker.enqueue_wa_workers",
                        _enqueue_capriccioso)

    esito = await cron_worker.wa_campaign_supervisor({})

    assert a["campaign"].id in visti and b["campaign"].id in visti
    assert esito["riaccodate"] >= 1


@pytest.mark.asyncio
async def test_b3_numero_in_cooldown_non_viene_riaccodato(db_session, enqueue_spia):
    """Un numero fermato da FM2 non deve essere risvegliato dal supervisore:
    sarebbe la terza porta sullo stesso cortile."""
    from app.models.wa import WaCampaignStatus, WaNumberStatus
    from app.workers import cron_worker

    ctx = await _scenario(db_session)
    ctx["campaign"].status = WaCampaignStatus.running
    ctx["number"].status = WaNumberStatus.cooldown
    await db_session.commit()

    await cron_worker.wa_campaign_supervisor({})
    assert ctx["campaign"].id not in enqueue_spia


@pytest.mark.asyncio
async def test_b4_contatto_optato_non_rende_riaccodabile_la_campagna(
        db_session, enqueue_spia):
    """Se l'unica riga rimasta e' di un contatto in opt-out, non c'e' lavoro:
    riaccodare aprirebbe il browser per non mandare niente."""
    from app.models.wa import WaCampaignStatus
    from app.workers import cron_worker

    ctx = await _scenario(db_session)
    ctx["campaign"].status = WaCampaignStatus.running
    ctx["contact"].opted_out = True
    ctx["contact"].do_not_contact = True
    await db_session.commit()

    await cron_worker.wa_campaign_supervisor({})
    assert ctx["campaign"].id not in enqueue_spia


@pytest.mark.asyncio
async def test_b5_riga_sotto_lock_fresco_non_viene_riaccodata(db_session, enqueue_spia):
    """Lock fresco = un worker ci sta gia' lavorando. Riaccodare qui non fa
    danni (ARQ deduplica) ma il conteggio direbbe il falso."""
    from app.models.wa import WaCampaignStatus
    from app.workers import cron_worker

    ctx = await _scenario(db_session)
    ctx["campaign"].status = WaCampaignStatus.running
    ctx["cc"].locked_by = "wa-vivo"
    ctx["cc"].locked_at = datetime.utcnow()
    await db_session.commit()

    try:
        await cron_worker.wa_campaign_supervisor({})
        assert ctx["campaign"].id not in enqueue_spia
    finally:
        # Stessa pulizia del test sul lock stantio qui sotto: un lock lasciato
        # a DB (anche fresco) invecchia mentre la suite continua, e in coda
        # farebbe fallire l'invariante I1 in un altro file.
        ctx["cc"].locked_by = None
        ctx["cc"].locked_at = None
        await db_session.commit()


@pytest.mark.asyncio
async def test_b6_lock_stantio_torna_riaccodabile(db_session, enqueue_spia):
    """Controprova del precedente: un lock piu' vecchio del timeout e' un
    worker morto, e quella riga e' lavoro da recuperare."""
    from app.config import settings
    from app.models.wa import WaCampaignStatus
    from app.workers import cron_worker

    ctx = await _scenario(db_session)
    ctx["campaign"].status = WaCampaignStatus.running
    ctx["cc"].locked_by = "wa-morto"
    ctx["cc"].locked_at = datetime.utcnow() - timedelta(
        minutes=int(settings.wa_lock_timeout_min) + 1)
    await db_session.commit()

    try:
        await cron_worker.wa_campaign_supervisor({})
        assert ctx["campaign"].id in enqueue_spia
    finally:
        # Il DB di test e' CONDIVISO fra i file della suite, e
        # test_zz_wa_qa_invariants verifica alla fine che non resti nessun lock
        # piu' vecchio del timeout (invariante I1). Un lock stantio fabbricato
        # qui e lasciato li' fa fallire quel test in coda alla suite, con un
        # rosso che sembra una regressione e sta in un altro file. Si pulisce
        # in `finally`, cosi' vale anche se l'assert sopra salta.
        ctx["cc"].locked_by = None
        ctx["cc"].locked_at = None
        await db_session.commit()


@pytest.mark.asyncio
async def test_b7_due_giri_concorrenti_del_supervisore(db_session, monkeypatch):
    """Due tick sovrapposti (cron lento + riavvio): nessuna eccezione, e la
    deduplica resta compito di ARQ -- qui si prova solo che non si rompa."""
    from app.models.wa import WaCampaignStatus
    from app.workers import cron_worker

    ctx = await _scenario(db_session)
    ctx["campaign"].status = WaCampaignStatus.running
    await db_session.commit()

    async def _enqueue(campaign_id: str, **kw) -> int:
        await asyncio.sleep(0)
        return 1

    monkeypatch.setattr("app.workers.wa_worker.enqueue_wa_workers", _enqueue)

    risultati = await asyncio.gather(
        cron_worker.wa_campaign_supervisor({}),
        cron_worker.wa_campaign_supervisor({}),
        return_exceptions=True,
    )
    for r in risultati:
        assert not isinstance(r, Exception), f"eccezione non gestita: {r!r}"


# ===========================================================================
# C — Recupero da 'error'
# ===========================================================================

@pytest.mark.asyncio
async def test_c1_doppio_recover_il_secondo_e_rifiutato(client, db_session):
    from app.models.wa import WaCampaignStatus

    ctx = await _scenario(db_session)
    ctx["campaign"].status = WaCampaignStatus.error
    await db_session.commit()

    primo = await client.post(f"/api/wa/campaigns/{ctx['campaign'].id}/recover",
                              json={"motivo": "sistemato"})
    secondo = await client.post(f"/api/wa/campaigns/{ctx['campaign'].id}/recover",
                                json={"motivo": "sistemato di nuovo"})

    assert primo.status_code == 200
    assert secondo.status_code == 409
    await db_session.refresh(ctx["campaign"])
    assert ctx["campaign"].status == WaCampaignStatus.paused


@pytest.mark.asyncio
async def test_c2_motivo_da_diecimila_caratteri_non_e_un_500(client, db_session):
    from app.models.wa import WaCampaignStatus

    ctx = await _scenario(db_session)
    ctx["campaign"].status = WaCampaignStatus.error
    await db_session.commit()

    r = await client.post(f"/api/wa/campaigns/{ctx['campaign'].id}/recover",
                          json={"motivo": "x" * 10_000})
    assert r.status_code in (200, 422), r.text


@pytest.mark.asyncio
async def test_c3_motivo_con_null_byte_e_unicode_ostile(client, db_session):
    from app.models.wa import WaCampaignStatus

    ctx = await _scenario(db_session)
    ctx["campaign"].status = WaCampaignStatus.error
    await db_session.commit()

    r = await client.post(
        f"/api/wa/campaigns/{ctx['campaign'].id}/recover",
        json={"motivo": "ok‮drop⁩ \U0001f600 '; DROP TABLE wa_campaigns;--"})
    assert r.status_code in (200, 422), r.text
    # Qualunque sia l'esito, la tabella deve esistere ancora.
    await db_session.refresh(ctx["campaign"])
    assert ctx["campaign"].status in (WaCampaignStatus.error, WaCampaignStatus.paused)


@pytest.mark.asyncio
async def test_c4_recover_di_una_campagna_inesistente(client):
    r = await client.post("/api/wa/campaigns/non-esiste/recover",
                          json={"motivo": "x"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_c5_recover_senza_body(client, db_session):
    from app.models.wa import WaCampaignStatus

    ctx = await _scenario(db_session)
    ctx["campaign"].status = WaCampaignStatus.error
    await db_session.commit()

    r = await client.post(f"/api/wa/campaigns/{ctx['campaign'].id}/recover")
    assert r.status_code == 422
    await db_session.refresh(ctx["campaign"])
    assert ctx["campaign"].status == WaCampaignStatus.error


@pytest.mark.asyncio
async def test_c6_recover_non_scavalca_le_validazioni_di_avvio(client, db_session):
    """Il recupero porta a paused, e da li' il resume rifiuta se il numero non
    e' attivo. Se recover portasse a running, una campagna ripartirebbe su un
    numero senza sessione WhatsApp."""
    from app.models.wa import WaCampaignStatus, WaNumberStatus

    ctx = await _scenario(db_session)
    ctx["campaign"].status = WaCampaignStatus.error
    ctx["number"].status = WaNumberStatus.qr_required
    await db_session.commit()

    await client.post(f"/api/wa/campaigns/{ctx['campaign'].id}/recover",
                      json={"motivo": "verificato"})
    r = await client.post(f"/api/wa/campaigns/{ctx['campaign'].id}/resume")

    assert r.status_code == 422
    await db_session.refresh(ctx["campaign"])
    assert ctx["campaign"].status == WaCampaignStatus.paused


# ===========================================================================
# D — Chiusura della campagna
# ===========================================================================

@pytest.mark.asyncio
async def test_d1_due_chiusure_concorrenti_un_solo_completed(db_session, monkeypatch):
    """Due mini-sessioni che finiscono insieme: la campagna si chiude una volta
    sola e l'avviso Telegram non parte due volte."""
    from app.models.wa import WaCampaignStatus, WaContactStatus
    from app.services import notifier
    from app.workers import wa_worker

    ctx = await _scenario(db_session)
    ctx["campaign"].status = WaCampaignStatus.running
    ctx["cc"].status = WaContactStatus.completed
    ctx["cc"].next_action_at = None
    await db_session.commit()

    avvisi: list[str] = []

    async def _telegram(msg, level="info", **kw):
        avvisi.append(msg)
    monkeypatch.setattr(notifier, "send_telegram", _telegram)

    esiti = await asyncio.gather(
        wa_worker._chiudi_campagna_se_finita(ctx["number"].id),
        wa_worker._chiudi_campagna_se_finita(ctx["number"].id),
        return_exceptions=True,
    )
    for e in esiti:
        assert not isinstance(e, Exception), f"eccezione non gestita: {e!r}"

    chiuse = [e for e in esiti if e == ctx["campaign"].id]
    assert len(chiuse) == 1, "la campagna deve chiudersi una volta sola"
    assert len(avvisi) == 1, "un solo avviso Telegram"

    await db_session.refresh(ctx["campaign"])
    assert ctx["campaign"].status == WaCampaignStatus.completed


@pytest.mark.asyncio
async def test_d2_numero_senza_campagne_non_esplode(db_session):
    from app.workers import wa_worker

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    await db_session.commit()

    assert await wa_worker._chiudi_campagna_se_finita(numero.id) is None


@pytest.mark.asyncio
async def test_d3_righe_terminali_diverse_non_impediscono_la_chiusura(db_session):
    """skipped/opted_out/replied sono terminali quanto completed: una campagna
    che ha solo quelle e' finita, non appesa."""
    from app.models.wa import WaCampaignStatus, WaContactStatus
    from app.workers import wa_worker

    ctx = await _scenario(db_session)
    ctx["campaign"].status = WaCampaignStatus.running
    ctx["cc"].status = WaContactStatus.skipped
    ctx["cc"].next_action_at = None

    altro = await make_contact(db_session, ctx["tenant"])
    cc2 = await make_campaign_contact(db_session, ctx["campaign"], altro,
                                      status=WaContactStatus.opted_out)
    cc2.next_action_at = None
    await db_session.commit()

    assert await wa_worker._chiudi_campagna_se_finita(ctx["number"].id) == ctx["campaign"].id


# ===========================================================================
# E — Avvio e accodamento
# ===========================================================================

@pytest.mark.asyncio
async def test_e1_una_seconda_campagna_rifiutata_non_accoda(db_session, enqueue_spia):
    """Il rischio introdotto da M5.1: `avvia()` ora accoda, quindi un avvio
    RIFIUTATO non deve comunque mettere un job in coda -- girerebbe un worker
    su una campagna che non e' partita.

    La concorrenza vera su questa regola (due `avvia()` in due sessioni DB
    indipendenti, TOCTOU riprodotto con asyncio.gather) e' gia' coperta da
    `test_due_avvia_concorrenti_sullo_stesso_numero_non_passano_entrambe` in
    test_wa_campaign_lifecycle.py: quel test costruisce due engine separati,
    cosa che qui non si puo' fare con la fixture `db_session` condivisa -- un
    AsyncSession non e' concorrente e il gather produce MissingGreenlet, che e'
    un difetto del test, non del codice. Qui si prova la regola nuova.
    """
    from app.models.wa import WaCampaign, WaCampaignStatus
    from app.services import wa_campaign_service as svc
    from sqlalchemy import func, select

    ctx = await _scenario(db_session)
    seconda, _ = await make_campaign(db_session, ctx["tenant"], ctx["number"],
                                     name="Seconda")
    altro = await make_contact(db_session, ctx["tenant"])
    await make_campaign_contact(db_session, seconda, altro)
    await db_session.commit()

    await svc.avvia(db_session, ctx["campaign"].id)
    with pytest.raises(ValueError):
        await svc.avvia(db_session, seconda.id)

    running = await db_session.scalar(
        select(func.count(WaCampaign.id)).where(
            WaCampaign.wa_number_id == ctx["number"].id,
            WaCampaign.status == WaCampaignStatus.running))
    assert running == 1, "max una campagna running per numero (SDD Q2)"
    assert enqueue_spia == [ctx["campaign"].id], (
        "solo la campagna partita davvero deve avere un worker in coda")


@pytest.mark.asyncio
async def test_f1_tre_numeri_non_su_whatsapp_non_fermano_il_numero(db_session, monkeypatch):
    """Il difetto che la review ha trovato nel fix stesso: 'ricerca senza
    risultati' e' un fatto sul CONTATTO (probabilmente non e' su WhatsApp), non
    sul nostro DOM -- `EsitoApertura.colpa_nostra` e' False. Tre di fila in una
    lista non devono fermare il numero per quattro ore con un Telegram che dice
    'probabile DOM cambiato'."""
    from app.config import settings
    from app.models.wa import WaCampaignStatus, WaNumberStatus
    from app.services.wa_sender import EsitoInvio
    from app.workers import wa_worker
    from tests.helpers_wa_tempo import orologio_virtuale

    monkeypatch.setattr(settings, "wa_send_enabled", True)
    orologio_virtuale(wa_worker, monkeypatch)

    ctx = await _scenario(db_session)
    ctx["campaign"].status = WaCampaignStatus.running
    for _ in range(3):
        altro = await make_contact(db_session, ctx["tenant"])
        await make_campaign_contact(db_session, ctx["campaign"], altro)
    await db_session.commit()

    async def _sempre_ricerca_vuota(*a, **kw):
        return EsitoInvio("queued", "ricerca_senza_risultati", arma_fm2=False)
    monkeypatch.setattr(wa_worker.wa_sender, "invia_a_contatto", _sempre_ricerca_vuota)
    monkeypatch.setattr(wa_worker, "_ora_locale_corrente", lambda: 11)

    import contextlib

    @contextlib.asynccontextmanager
    async def _ctx(*a, **kw):
        class _C:
            async def new_page(self):
                class _P:
                    async def goto(self, *a, **kw): return None
                return _P()
        yield _C()
    monkeypatch.setattr(wa_worker, "_open_wa_browser", _ctx)
    monkeypatch.setattr(wa_worker, "WhatsAppWebPage", lambda page: object())

    @contextlib.asynccontextmanager
    async def _lock(number_id, **kw):
        yield "tok"
    monkeypatch.setattr(wa_worker.wa_profile_lock, "held", _lock)

    async def _renew(number_id, token, **kw):
        return True
    monkeypatch.setattr(wa_worker.wa_profile_lock, "renew", _renew)

    esito = await wa_worker.esegui_mini_sessione(ctx["number"].id)

    assert esito["motivo"] != "guasti_consecutivi", (
        "tre contatti non su WhatsApp non sono un DOM rotto")
    await db_session.refresh(ctx["number"])
    assert ctx["number"].status == WaNumberStatus.active
    await db_session.refresh(ctx["campaign"])
    assert ctx["campaign"].status == WaCampaignStatus.running


@pytest.mark.asyncio
async def test_f2_un_re_qr_esplicito_toglie_il_cooldown(db_session, monkeypatch):
    """La guardia sul cooldown deve fermare solo le LETTURE automatiche.
    Un operatore che riscansiona il QR di persona sta facendo l'azione
    esplicita di cui parla il commento su _STATI_PROTETTI_DA_RESURREZIONE, e
    deve poter rimettere il numero in gioco -- altrimenti l'unica uscita
    resterebbe cancellare a mano una chiave Redis."""
    from app.models.wa import WaNumberStatus
    from app.services import wa_session

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant, status=WaNumberStatus.cooldown)
    await db_session.commit()

    # Lettura automatica (health-check): non promuove.
    await wa_session._persist_status(numero.id, WaNumberStatus.active)
    await db_session.refresh(numero)
    assert numero.status == WaNumberStatus.cooldown

    # Atto esplicito dell'operatore (assisted_login): promuove.
    await wa_session._persist_status(numero.id, WaNumberStatus.active,
                                     da_lettura_automatica=False)
    await db_session.refresh(numero)
    assert numero.status == WaNumberStatus.active


@pytest.mark.asyncio
async def test_f3_avvia_su_numero_in_cooldown_non_parla_di_qr(db_session):
    """Dopo un FM2 la campagna e' in error E il numero in cooldown: il messaggio
    d'errore del resume non deve mandare l'operatore a rifare un QR che non
    serve, mentre la sessione e' viva."""
    from app.models.wa import WaCampaignStatus, WaNumberStatus
    from app.services import wa_campaign_service as svc

    ctx = await _scenario(db_session)
    ctx["campaign"].status = WaCampaignStatus.paused
    ctx["number"].status = WaNumberStatus.cooldown
    await db_session.commit()

    with pytest.raises(ValueError) as exc:
        await svc.avvia(db_session, ctx["campaign"].id)

    testo = str(exc.value).lower()
    assert "cooldown" in testo
    assert "qr" not in testo or "non serve rifare il qr" in testo


@pytest.mark.asyncio
async def test_f4_recover_dice_che_il_numero_e_in_cooldown(client, db_session):
    from app.models.wa import WaCampaignStatus, WaNumberStatus

    ctx = await _scenario(db_session)
    ctx["campaign"].status = WaCampaignStatus.error
    ctx["number"].status = WaNumberStatus.cooldown
    await db_session.commit()

    r = await client.post(f"/api/wa/campaigns/{ctx['campaign'].id}/recover",
                          json={"motivo": "verificato"})
    assert r.status_code == 200
    body = r.json()
    assert body["stato_numero"] == "cooldown"
    assert "cooldown" in body["prossimo_passo"]


@pytest.mark.asyncio
async def test_f5_il_motivo_non_viene_troncato_in_silenzio(client, db_session, caplog):
    """max_length=500 accettati, poi 200 scritti: la traccia che il campo esiste
    per lasciare risultava mutilata senza dirlo."""
    from app.models.wa import WaCampaignStatus

    ctx = await _scenario(db_session)
    ctx["campaign"].status = WaCampaignStatus.error
    await db_session.commit()

    motivo = "M" * 400
    r = await client.post(f"/api/wa/campaigns/{ctx['campaign'].id}/recover",
                          json={"motivo": motivo})
    assert r.status_code == 200

    r2 = await client.post(f"/api/wa/campaigns/{ctx['campaign'].id}/recover",
                           json={"motivo": "N" * 501})
    assert r2.status_code == 422, "oltre il limite dichiarato: errore, non taglio"


@pytest.mark.asyncio
async def test_e2_una_campagna_senza_contatti_non_parte_e_non_accoda(
        db_session, enqueue_spia):
    from app.services import wa_campaign_service as svc

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    campagna, _ = await make_campaign(db_session, tenant, numero)
    await db_session.commit()

    with pytest.raises(ValueError):
        await svc.avvia(db_session, campagna.id)

    assert enqueue_spia == []


@pytest.mark.asyncio
async def test_headless_dell_invio_viene_dalla_config_non_e_cablato(db_session, monkeypatch):
    """Il browser di invio deve poter essere VISIBILE, per collaudo.

    `esegui_mini_sessione` chiamava `_open_wa_browser(..., headless=True)` con
    il True scritto nel codice: `HEADLESS=false` nell'.env non aveva alcun
    effetto sull'invio (vale per il login assistito, che passa False
    esplicito, e per la Fase A discover, che ha il parametro -- non qui).
    Guardare il primo messaggio partire con i propri occhi era impossibile
    senza modificare il sorgente.

    Il default resta True: un worker che gira senza nessuno davanti non deve
    aprire finestre.
    """
    import contextlib

    from app.config import settings
    from app.models.wa import WaCampaignStatus
    from app.workers import wa_worker

    monkeypatch.setattr(settings, "wa_send_enabled", True)
    monkeypatch.setattr(settings, "wa_send_headless", False)

    ctx = await _scenario(db_session)
    ctx["campaign"].status = WaCampaignStatus.running
    await db_session.commit()

    monkeypatch.setattr(wa_worker, "_ora_locale_corrente", lambda: 11)

    visto = {}

    class _Basta(Exception):
        pass

    def _registra(*a, **kw):
        visto.update(kw)
        raise _Basta()

    monkeypatch.setattr(wa_worker, "_open_wa_browser", _registra)

    @contextlib.asynccontextmanager
    async def _lock(number_id, **kw):
        yield "tok"
    monkeypatch.setattr(wa_worker.wa_profile_lock, "held", _lock)

    try:
        await wa_worker.esegui_mini_sessione(ctx["number"].id)
    except Exception:
        pass   # interessa solo COME e' stato chiamato il browser

    assert visto, "il browser non e' stato aperto: il test non ha misurato niente"
    assert visto["headless"] is False, (
        f"headless cablato: atteso False da settings.wa_send_headless, visto {visto['headless']!r}"
    )
