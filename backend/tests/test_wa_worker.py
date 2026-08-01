import asyncio
import uuid
from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from app.workers import wa_worker


async def _scenario_claim(db_session, e164: str = "+393331112223", contatti: int = 1):
    """Tenant + numero + contatto + campagna running + step 0, tutto a DB.
    Identico a _scenario_invio del Task 8 (test_wa_sender.py) piu'
    next_action_at nel passato sulla riga wa_campaign_contacts: copiato
    apposta invece di importato, cosi' questo file resta leggibile da solo.

    `contatti` (default 1, retro-compatibile con i test di Task 10) crea N
    contatti/righe wa_campaign_contacts DISTINTI sulla stessa campagna: serve
    a Task 11 per provare l'escalation "guasti su chat diverse" (MAX_GUASTI_
    CONSECUTIVI), che deve restare vera anche con piu' di una chat nel pool."""
    from app.models.tenant import Tenant
    from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                               WaCampaignType, WaContact, WaContactStatus, WaNumber,
                               WaNumberStatus, WaSendCondition, WaSequenceStep)
    from app.utils.crypto import encrypt
    from app.utils.phone_pseudonym import hmac_phone

    tenant = Tenant(id=str(uuid.uuid4()), name="T", status="active")
    db_session.add(tenant)
    await db_session.flush()
    number = WaNumber(id=str(uuid.uuid4()), tenant_id=tenant.id, label="n",
                      phone_hmac=f"n-{uuid.uuid4()}", encrypted_phone=encrypt("+390000000000"),
                      status=WaNumberStatus.active)
    db_session.add(number)
    await db_session.flush()
    campaign = WaCampaign(id=str(uuid.uuid4()), tenant_id=tenant.id,
                          wa_number_id=number.id, name="c",
                          campaign_type=WaCampaignType.marketing,
                          status=WaCampaignStatus.running, optout_enabled=True,
                          optout_cta="Scrivi STOP per non ricevere piu' messaggi.")
    db_session.add(campaign)
    await db_session.flush()
    step = WaSequenceStep(id=str(uuid.uuid4()), campaign_id=campaign.id, step_index=0,
                          template_a="Ciao {nome}, promo attiva.",
                          send_condition=WaSendCondition.always, wait_days=0)
    db_session.add(step)
    await db_session.flush()

    contacts, ccs = [], []
    for i in range(contatti):
        e = e164 if i == 0 else f"{e164}{i}"
        c = WaContact(id=str(uuid.uuid4()), tenant_id=tenant.id,
                     phone_hmac=hmac_phone(e), encrypted_phone=encrypt(e),
                     display_name="Marco")
        db_session.add(c)
        await db_session.flush()
        cc = WaCampaignContact(id=str(uuid.uuid4()), campaign_id=campaign.id,
                               contact_id=c.id, status=WaContactStatus.queued,
                               current_step=-1,
                               next_action_at=datetime.utcnow() - timedelta(minutes=1))
        db_session.add(cc)
        contacts.append(c)
        ccs.append(cc)
    await db_session.commit()
    return {"tenant": tenant, "number": number, "contact": contacts[0],
            "campaign": campaign, "step": step, "cc": ccs[0],
            "contacts": contacts, "ccs": ccs}


@pytest.mark.asyncio
async def test_claim_prende_la_riga_pronta(db_session):
    ctx = await _scenario_claim(db_session)
    preso = await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1")
    assert preso is not None
    cc, contact, campaign, step = preso
    assert cc.id == ctx["cc"].id
    assert cc.locked_by == "w1"
    assert step.step_index == 0


@pytest.mark.asyncio
async def test_claim_ignora_next_action_at_null(db_session):
    """Invariante I3 del contratto: una riga senza appuntamento non e' una
    riga da inviare subito, e' una riga rotta. Fail-closed."""
    ctx = await _scenario_claim(db_session)
    ctx["cc"].next_action_at = None
    await db_session.commit()
    assert await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1") is None


@pytest.mark.asyncio
async def test_claim_ignora_contatto_optato_fuori(db_session):
    """M2 filtra all'ingest, ma fra ingest e invio passano settimane
    (invariante I4): si ricontrolla live."""
    ctx = await _scenario_claim(db_session)
    ctx["contact"].opted_out = True
    await db_session.commit()
    assert await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1") is None


@pytest.mark.asyncio
async def test_claim_ignora_campagna_non_running_e_numero_non_active(db_session):
    from app.models.wa import WaCampaignStatus, WaNumberStatus
    ctx = await _scenario_claim(db_session)
    ctx["campaign"].status = WaCampaignStatus.paused
    await db_session.commit()
    assert await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1") is None

    ctx["campaign"].status = WaCampaignStatus.running
    ctx["number"].status = WaNumberStatus.qr_required
    await db_session.commit()
    assert await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1") is None


@pytest.mark.asyncio
async def test_claim_rispetta_lock_fresco_e_recupera_lock_stale(db_session):
    ctx = await _scenario_claim(db_session)
    ctx["cc"].locked_by = "altro-worker"
    ctx["cc"].locked_at = datetime.utcnow()
    await db_session.commit()
    assert await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1") is None

    # lock vecchio di 21 minuti: la sessione che lo teneva e' morta
    ctx["cc"].locked_at = datetime.utcnow() - timedelta(minutes=21)
    await db_session.commit()
    preso = await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1")
    assert preso is not None and preso[0].locked_by == "w1"


@pytest.mark.asyncio
async def test_due_worker_concorrenti_non_prendono_la_stessa_riga(db_session):
    """Concorrenza VERA (gather), non due chiamate in fila: e' l'unico modo
    in cui il bug si manifesta."""
    from app.database import AsyncSessionLocal
    ctx = await _scenario_claim(db_session)

    async def _claim(worker_id):
        async with AsyncSessionLocal() as db:
            return await wa_worker.claim_next_wa_contact(
                db, number_id=ctx["number"].id, worker_id=worker_id)

    a, b = await asyncio.gather(_claim("w1"), _claim("w2"))
    assert (a is None) != (b is None), "esattamente uno dei due deve vincere"


@pytest.mark.asyncio
async def test_claim_ignora_contatto_oltre_soglia_fallimenti(db_session, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "wa_max_failures_per_contact", 3)
    ctx = await _scenario_claim(db_session)
    ctx["cc"].failure_count = 3
    await db_session.commit()
    assert await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1") is None


async def _mini_sessione_con_doppi(db_session, monkeypatch, *, contatti=1,
                                   budget=True, ora_corrente=12,
                                   fake_invio=None, halted_getter=None,
                                   _ctx_out: dict | None = None):
    """Esercita esegui_mini_sessione con browser/POM/orologio finti.
    Il browser vero e' esercitato SOLO nel Task 15 (prova dal vivo): qui si
    prova la LOGICA, che e' dove hanno abitato tutti i difetti di M1."""
    import contextlib
    from app.config import settings
    from app.services import bot_state_service as bss, wa_number_manager as wnm
    from app.services.wa_sender import EsitoInvio

    monkeypatch.setattr(settings, "wa_send_enabled", True)

    async def _halted(db=None):
        return halted_getter() if halted_getter else False
    monkeypatch.setattr(bss, "is_wa_halted", _halted)

    async def _budget(*a, **kw):
        return budget
    monkeypatch.setattr(wnm, "has_wa_send_budget", _budget)

    @contextlib.asynccontextmanager
    async def _ctx(*a, **kw):
        class _Ctx:
            async def new_page(self):
                class _P:
                    async def goto(self, *a, **kw): return None
                return _P()
        yield _Ctx()
    monkeypatch.setattr(wa_worker, "_open_wa_browser", _ctx)
    monkeypatch.setattr(wa_worker, "_ora_locale_corrente", lambda: ora_corrente)
    monkeypatch.setattr(wa_worker, "WhatsAppWebPage", lambda page: object())

    async def _invio(*a, **kw):
        return EsitoInvio("sent", "ok")
    monkeypatch.setattr(wa_worker.wa_sender, "invia_a_contatto",
                        fake_invio or _invio)
    monkeypatch.setattr(wa_worker.wa_timing, "wa_send_delay_seconds", lambda: 0.0)
    monkeypatch.setattr(wa_worker.wa_timing, "wa_session_message_count", lambda c: contatti)

    ctx = await _scenario_claim(db_session, contatti=contatti)
    if _ctx_out is not None:
        _ctx_out.update(ctx)
    return await wa_worker.esegui_mini_sessione(ctx["number"].id)


@pytest.mark.asyncio
async def test_send_enabled_false_non_apre_nemmeno_il_browser(db_session, monkeypatch):
    """Il master switch sta SOPRA tutto: a false non si apre un browser,
    non si claima una riga, non si tocca niente."""
    from app.config import settings
    monkeypatch.setattr(settings, "wa_send_enabled", False)
    aperto = {"si": False}

    def _boom(*a, **kw):
        aperto["si"] = True
        raise AssertionError("browser aperto con WA_SEND_ENABLED=false")

    monkeypatch.setattr(wa_worker, "_open_wa_browser", _boom)
    esito = await wa_worker.esegui_mini_sessione("num-x")
    assert esito["motivo"] == "send_disabled"
    assert aperto["si"] is False


@pytest.mark.asyncio
async def test_kill_switch_wa_ferma_la_sessione(db_session, monkeypatch):
    from app.config import settings
    from app.services import bot_state_service as bss
    monkeypatch.setattr(settings, "wa_send_enabled", True)

    async def _halted(db=None):
        return True
    monkeypatch.setattr(bss, "is_wa_halted", _halted)
    esito = await wa_worker.esegui_mini_sessione("num-x")
    assert esito["motivo"] == "wa_halted"


@pytest.mark.asyncio
async def test_kill_switch_acceso_a_meta_sessione_interrompe_dopo_il_messaggio_corrente(
        db_session, monkeypatch):
    """FM15: tutto si ferma entro il job corrente. Non a meta' di un invio:
    dopo quello in corso."""
    stato = {"halted": False, "inviati": 0}

    async def _fake_invio(*a, **kw):
        stato["inviati"] += 1
        stato["halted"] = True      # qualcuno preme il kill-switch adesso
        from app.services.wa_sender import EsitoInvio
        return EsitoInvio("sent", "ok")

    esito = await _mini_sessione_con_doppi(
        db_session, monkeypatch, fake_invio=_fake_invio,
        halted_getter=lambda: stato["halted"], contatti=5)
    assert stato["inviati"] == 1
    assert esito["motivo"] == "wa_halted"


@pytest.mark.asyncio
async def test_cap_raggiunto_esce_con_defer_e_non_marca_i_contatti(db_session, monkeypatch):
    """Cap non e' un fallimento dei contatti: le righe restano queued."""
    esito = await _mini_sessione_con_doppi(
        db_session, monkeypatch, budget=False, contatti=3)
    assert esito["motivo"] == "cap_esaurito"
    assert esito["inviati"] == 0
    assert esito["falliti"] == 0


@pytest.mark.asyncio
async def test_fuori_finestra_oraria_non_invia(db_session, monkeypatch):
    esito = await _mini_sessione_con_doppi(
        db_session, monkeypatch, ora_corrente=4, contatti=3)
    assert esito["motivo"] == "fuori_finestra"


@pytest.mark.asyncio
async def test_tre_guasti_nostri_consecutivi_fermano_il_numero(db_session, monkeypatch):
    """FM2: selettori rotti -> stop invii del numero e campagna in error.
    I contatti NON diventano failed: e' colpa nostra."""
    from app.services.wa_sender import EsitoInvio

    async def _sempre_guasto(*a, **kw):
        return EsitoInvio("queued", "casella-ricerca-non-trovata")

    esito = await _mini_sessione_con_doppi(
        db_session, monkeypatch, fake_invio=_sempre_guasto, contatti=5)
    assert esito["motivo"] == "guasti_consecutivi"
    assert esito["inviati"] == 0
    assert esito["falliti"] == 0


@pytest.mark.asyncio
async def test_eccezione_imprevista_in_invia_a_contatto_non_rompe_la_mini_sessione(
        db_session, monkeypatch):
    """Fix 4 (review finale I7): un'eccezione IMPREVISTA da invia_a_contatto
    (decrypt fallito, commit DB, blip) non deve propagare fuori dal job --
    prima usciva dall'intera mini-sessione e da wa_send_task in silenzio, il
    contatto restava lockato fino al prossimo health-check (20 min) e
    _rischedula non veniva MAI raggiunta: il numero smetteva di inviare
    senza un log che lo spiegasse. Ora si logga, si rilascia il lock, e
    conta come guasto verso l'escalation FM2 -- stesso trattamento degli
    altri path 'colpa nostra'."""
    from sqlalchemy import select
    from app.models.wa import WaCampaignContact

    async def _fake_invio_rotto(*a, **kw):
        raise RuntimeError("decrypt fallito")

    ctx_out: dict = {}
    esito = await _mini_sessione_con_doppi(
        db_session, monkeypatch, fake_invio=_fake_invio_rotto, contatti=5,
        _ctx_out=ctx_out)

    assert esito["motivo"] == "guasti_consecutivi"
    assert esito["inviati"] == 0
    assert esito["falliti"] == 0

    cc = await db_session.scalar(
        select(WaCampaignContact).where(WaCampaignContact.id == ctx_out["cc"].id))
    assert cc.locked_by is None and cc.locked_at is None


# ---------------------------------------------------------------------------
# QA M3 (Task 15 Step 3): items funzionali 7, 15, 17, 18, 20, 22 e
# adversarial D20/D21, G26/G27.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_7_numero_non_attivo_blocca_prima_del_browser(db_session, monkeypatch):
    """QA item 7: il cancello 'numero attivo' si verifica PRIMA di aprire
    il contesto Playwright."""
    from app.config import settings
    from app.models.tenant import Tenant
    from app.models.wa import WaNumber, WaNumberStatus
    from app.services import bot_state_service as bss
    from app.utils.crypto import encrypt

    monkeypatch.setattr(settings, "wa_send_enabled", True)

    async def _halted(db=None):
        return False
    monkeypatch.setattr(bss, "is_wa_halted", _halted)

    aperto = {"si": False}

    def _boom(*a, **kw):
        aperto["si"] = True
        raise AssertionError("browser aperto con numero non attivo")
    monkeypatch.setattr(wa_worker, "_open_wa_browser", _boom)

    tenant = Tenant(id=str(uuid.uuid4()), name="T7", status="active")
    db_session.add(tenant)
    await db_session.flush()
    number = WaNumber(id=str(uuid.uuid4()), tenant_id=tenant.id, label="n",
                      phone_hmac=f"h-{uuid.uuid4()}", encrypted_phone=encrypt("+390000000001"),
                      status=WaNumberStatus.pending_qr)
    db_session.add(number)
    await db_session.commit()

    esito = await wa_worker.esegui_mini_sessione(number.id)
    assert esito["motivo"] == "numero_non_attivo"
    assert aperto["si"] is False


@pytest.mark.asyncio
async def test_15_cap_raggiunto_rilascia_il_lock(db_session, monkeypatch):
    """QA item 15: il claim prende la riga, ma il cap la rilascia
    (locked_by/locked_at tornano NULL), non la consuma."""
    from sqlalchemy import select
    from app.models.wa import WaCampaignContact

    ctx_out: dict = {}
    esito = await _mini_sessione_con_doppi(
        db_session, monkeypatch, budget=False, contatti=1, _ctx_out=ctx_out)
    assert esito["motivo"] == "cap_esaurito"

    cc = await db_session.scalar(
        select(WaCampaignContact).where(WaCampaignContact.id == ctx_out["cc"].id))
    assert cc.locked_by is None and cc.locked_at is None


@pytest.mark.asyncio
async def test_17_fuori_finestra_rilascia_il_lock(db_session, monkeypatch):
    """QA item 17: stesso principio del 15, sul cancello finestra oraria."""
    from sqlalchemy import select
    from app.models.wa import WaCampaignContact

    ctx_out: dict = {}
    esito = await _mini_sessione_con_doppi(
        db_session, monkeypatch, ora_corrente=4, contatti=1, _ctx_out=ctx_out)
    assert esito["motivo"] == "fuori_finestra"

    cc = await db_session.scalar(
        select(WaCampaignContact).where(WaCampaignContact.id == ctx_out["cc"].id))
    assert cc.locked_by is None and cc.locked_at is None


@pytest.mark.asyncio
async def test_18_wa_send_task_rischedula_con_il_break_non_subito(db_session, monkeypatch):
    """QA item 18: dopo un'uscita per cap/finestra/completamento, wa_send_task
    rischedula con il break (minuti), non con un retry immediato."""
    catturato = {}

    async def _fake_mini(number_id):
        return {"inviati": 0, "falliti": 0, "saltati": 0, "motivo": "cap_esaurito"}
    monkeypatch.setattr(wa_worker, "esegui_mini_sessione", _fake_mini)

    async def _fake_campagna(number_id):
        return None
    monkeypatch.setattr(wa_worker, "_campagna_attiva_del_numero", _fake_campagna)
    monkeypatch.setattr(wa_worker.wa_timing, "wa_session_break_seconds", lambda c: 1234.0)

    async def _fake_rischedula(number_id, *, defer_seconds):
        catturato["number_id"] = number_id
        catturato["defer_seconds"] = defer_seconds
    monkeypatch.setattr(wa_worker, "_rischedula", _fake_rischedula)

    await wa_worker.wa_send_task({}, "num-x")
    assert catturato == {"number_id": "num-x", "defer_seconds": 1234}


@pytest.mark.asyncio
async def test_18b_wa_send_task_non_rischedula_su_motivi_terminali(db_session, monkeypatch):
    chiamato = {"n": 0}

    async def _fake_rischedula(number_id, *, defer_seconds):
        chiamato["n"] += 1
    monkeypatch.setattr(wa_worker, "_rischedula", _fake_rischedula)

    for motivo in ("send_disabled", "wa_halted", "numero_non_attivo",
                   "guasti_consecutivi", "niente_da_fare"):
        async def _fake_mini(number_id, _m=motivo):
            return {"motivo": _m}
        monkeypatch.setattr(wa_worker, "esegui_mini_sessione", _fake_mini)
        await wa_worker.wa_send_task({}, "num-x")
    assert chiamato["n"] == 0


@pytest.mark.asyncio
async def test_20_recovery_avvio_chiude_i_sending_appesi(db_session):
    """QA item 20: nessuna chiamata al browser, i messaggi 'sending' vanno
    failed e i lock si rilasciano."""
    from app.models.tenant import Tenant
    from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                               WaCampaignType, WaContact, WaContactStatus, WaMessage,
                               WaMessageStatus, WaNumber)

    tenant = Tenant(id=str(uuid.uuid4()), name="T20", status="active")
    db_session.add(tenant)
    await db_session.flush()
    contact = WaContact(id=str(uuid.uuid4()), tenant_id=tenant.id,
                        phone_hmac=f"h-{uuid.uuid4()}", encrypted_phone="e")
    number = WaNumber(id=str(uuid.uuid4()), tenant_id=tenant.id, label="n",
                      phone_hmac=f"h2-{uuid.uuid4()}", encrypted_phone="e")
    db_session.add_all([contact, number])
    await db_session.flush()
    campaign = WaCampaign(id=str(uuid.uuid4()), tenant_id=tenant.id, wa_number_id=number.id,
                          name="c20", campaign_type=WaCampaignType.followup,
                          status=WaCampaignStatus.running)
    db_session.add(campaign)
    await db_session.flush()
    cc = WaCampaignContact(id=str(uuid.uuid4()), campaign_id=campaign.id, contact_id=contact.id,
                           status=WaContactStatus.queued, current_step=0,
                           locked_by="worker-crashato", locked_at=datetime.utcnow())
    msg = WaMessage(id=str(uuid.uuid4()), campaign_id=campaign.id, contact_id=contact.id,
                    wa_number_id=number.id, step_index=0, template_variant="a",
                    rendered_text="ciao", status=WaMessageStatus.sending)
    db_session.add_all([cc, msg])
    await db_session.commit()

    n = await wa_worker.recover_wa_sending_on_startup()
    assert n == 1

    await db_session.refresh(msg)
    assert msg.status == WaMessageStatus.failed
    assert "recovery" in msg.error

    await db_session.refresh(cc)
    assert cc.locked_by is None and cc.locked_at is None


@pytest.mark.asyncio
async def test_22_campagna_paused_dal_inizio_esce_niente_da_fare(db_session, monkeypatch):
    """QA item 22: mini-sessione intera, non solo il claim -- esce pulita
    con motivo=niente_da_fare, zero invii."""
    from app.models.wa import WaCampaignStatus

    ctx = await _scenario_claim(db_session)
    ctx["campaign"].status = WaCampaignStatus.paused
    await db_session.commit()

    import contextlib
    from app.config import settings
    from app.services import bot_state_service as bss
    monkeypatch.setattr(settings, "wa_send_enabled", True)

    async def _halted(db=None):
        return False
    monkeypatch.setattr(bss, "is_wa_halted", _halted)

    @contextlib.asynccontextmanager
    async def _ctx_browser(*a, **kw):
        class _Ctx:
            async def new_page(self):
                class _P:
                    async def goto(self, *a, **kw): return None
                return _P()
        yield _Ctx()
    monkeypatch.setattr(wa_worker, "_open_wa_browser", _ctx_browser)
    monkeypatch.setattr(wa_worker, "WhatsAppWebPage", lambda page: object())

    esito = await wa_worker.esegui_mini_sessione(ctx["number"].id)
    assert esito["motivo"] == "niente_da_fare"
    assert esito["inviati"] == 0 and esito["falliti"] == 0 and esito["saltati"] == 0


@pytest.mark.asyncio
async def test_g26_campagna_paused_a_meta_sessione_ferma_pulito(db_session, monkeypatch):
    """Adversarial #26: la campagna va in pausa FRA un'iterazione e l'altra,
    non prima di avviare il loop. Il messaggio gia' in volo completa; il
    giro successivo trova niente."""
    from app.services.wa_sender import EsitoInvio
    from app.models.wa import WaCampaign, WaCampaignStatus
    from sqlalchemy import update

    stato = {"chiamate": 0}

    async def _fake_invio(db, pom, *, campaign, step, cc, contact, number,
                          browser_avviato_da_s):
        stato["chiamate"] += 1
        if stato["chiamate"] == 1:
            await db.execute(update(WaCampaign).where(WaCampaign.id == campaign.id)
                             .values(status=WaCampaignStatus.paused))
            await db.commit()
        return EsitoInvio("sent", "ok")

    esito = await _mini_sessione_con_doppi(
        db_session, monkeypatch, contatti=5, fake_invio=_fake_invio)
    assert stato["chiamate"] == 1
    assert esito["motivo"] == "niente_da_fare"
    assert esito["inviati"] == 1


@pytest.mark.asyncio
async def test_g27_numero_qr_required_a_meta_sessione_ferma_pulito(db_session, monkeypatch):
    """Adversarial #27: stesso schema del #26, sul numero invece che sulla
    campagna."""
    from app.services.wa_sender import EsitoInvio
    from app.models.wa import WaNumber, WaNumberStatus
    from sqlalchemy import update

    stato = {"chiamate": 0}

    async def _fake_invio(db, pom, *, campaign, step, cc, contact, number,
                          browser_avviato_da_s):
        stato["chiamate"] += 1
        if stato["chiamate"] == 1:
            await db.execute(update(WaNumber).where(WaNumber.id == number.id)
                             .values(status=WaNumberStatus.qr_required))
            await db.commit()
        return EsitoInvio("sent", "ok")

    esito = await _mini_sessione_con_doppi(
        db_session, monkeypatch, contatti=5, fake_invio=_fake_invio)
    assert stato["chiamate"] == 1
    assert esito["motivo"] == "niente_da_fare"
    assert esito["inviati"] == 1


@pytest_asyncio.fixture
async def _redis_o_skip():
    """Redis reale e' un requisito duro per questo test (adversarial #20):
    se non e' raggiungibile (es. CI senza servizio Redis), skip esplicito
    con motivo chiaro invece di un rosso che sembra una regressione.
    Duplicata in test_wa_ops_api.py (conftest.py e' congelato, §8.1)."""
    import arq
    from app.services.work_enqueue import arq_redis_settings
    try:
        pool = await arq.create_pool(arq_redis_settings())
        await pool.ping()
        await pool.aclose()
    except Exception as exc:
        pytest.skip(f"Redis non raggiungibile, test saltato: {type(exc).__name__}: {exc}")


@pytest.mark.asyncio
async def test_d20_due_enqueue_job_stesso_job_id_arq_scarta_il_duplicato(db_session, _redis_o_skip):
    """Adversarial #20: un solo job attivo per numero -- niente doppia
    mini-sessione parallela sullo stesso numero (romperebbe il pacing)."""
    import arq
    from app.services.work_enqueue import arq_redis_settings

    ctx = await _scenario_claim(db_session)
    await db_session.commit()

    n1 = await wa_worker.enqueue_wa_workers(ctx["campaign"].id)
    n2 = await wa_worker.enqueue_wa_workers(ctx["campaign"].id)
    assert n1 == 1
    assert n2 == 0

    redis = await arq.create_pool(arq_redis_settings())
    try:
        job_id = wa_worker.wa_send_job_id(ctx["number"].id)
        await redis.zrem("arq:queue", job_id)
        await redis.delete(f"arq:job:{job_id}")
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_ferma_numero_per_guasto_applica_cooldown_redis(db_session, monkeypatch):
    """Fix 3 (review finale I4): _ferma_numero_per_guasto scriveva
    WaNumber.status=cooldown a DB ma non chiamava mai apply_wa_cooldown --
    senza la chiave Redis con TTL, il prossimo health-check
    (release_expired_wa_cooldowns) la trova gia' "scaduta" e rimette il
    numero active da solo entro 30 min, annullando lo stop FM2."""
    from app.services import wa_number_manager as wnm

    ctx = await _scenario_claim(db_session)

    chiamate = []

    async def _fake_apply(number_id, *, minutes):
        chiamate.append((number_id, minutes))
    monkeypatch.setattr(wnm, "apply_wa_cooldown", _fake_apply)

    await wa_worker._ferma_numero_per_guasto(
        db_session, ctx["number"].id, ctx["campaign"].id, 3)

    assert len(chiamate) == 1
    numero_chiamato, minuti = chiamate[0]
    assert numero_chiamato == ctx["number"].id
    # deve richiedere un intervento umano, non auto-risolversi come i
    # cooldown brevi di routine (che sono nell'ordine dei minuti/30min)
    assert minuti >= 60


@pytest.mark.asyncio
async def test_d21_claim_confine_esatto_lock_timeout(db_session, monkeypatch):
    """Adversarial #21: 19min59s ancora fresco (rowcount=0), 20min01s stale
    (rowcount=1) -- nessuno scarto silenzioso di un secondo."""
    from app.config import settings
    monkeypatch.setattr(settings, "wa_lock_timeout_min", 20)

    ctx = await _scenario_claim(db_session)
    ctx["cc"].locked_by = "altro-worker"
    ctx["cc"].locked_at = datetime.utcnow() - timedelta(minutes=19, seconds=59)
    await db_session.commit()
    assert await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1") is None

    ctx["cc"].locked_at = datetime.utcnow() - timedelta(minutes=20, seconds=1)
    await db_session.commit()
    preso = await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1")
    assert preso is not None and preso[0].locked_by == "w1"
