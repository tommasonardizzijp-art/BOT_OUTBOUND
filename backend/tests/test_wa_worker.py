import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

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
                                   fake_invio=None, halted_getter=None):
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
