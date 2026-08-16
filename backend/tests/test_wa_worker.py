import asyncio
import uuid
from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from app.utils.tempo import adesso_utc
from app.workers import wa_worker
from tests.helpers_wa_tempo import fette_di_quarantena, orologio_virtuale


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
                               next_action_at=adesso_utc() - timedelta(minutes=1))
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
    # locked_at AWARE: il claim e' un UPDATE ORM con synchronize_session
    # 'evaluate', che RIVALUTA la WHERE in Python contro gli oggetti gia' in
    # sessione -- cioe' contro questa riga. Un naive scritto qui viene
    # confrontato con l'istante aware del worker e alza TypeError. Su
    # PostgreSQL non capita: la colonna e' timestamptz e rilegge sempre aware.
    # Questa riga simula cio' che la produzione scrive, e da oggi la
    # produzione scrive aware (app/utils/tempo.py).
    ctx = await _scenario_claim(db_session)
    ctx["cc"].locked_by = "altro-worker"
    ctx["cc"].locked_at = adesso_utc()
    await db_session.commit()
    assert await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1") is None

    # lock vecchio di 21 minuti: la sessione che lo teneva e' morta
    ctx["cc"].locked_at = adesso_utc() - timedelta(minutes=21)
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
async def test_claim_current_step_zero_avanza_a_step_uno_non_ristagna(db_session):
    """Fix 5 (review finale, dormiente ma un-liner): (cc.current_step or -1)
    ha la trappola falsy-zero di Python -- con current_step=0 (contatto che
    ha GIA' completato lo step 0), '0 or -1' vale -1 (0 e' falsy), quindi
    -1+1=0 riseleziona lo STESSO step 0 invece di avanzare all'1. Dormiente
    sotto l'MVP a singolo step, ma un vincolo di codice fragile da lasciare
    documentato invece che corretto."""
    from app.models.wa import WaContactStatus, WaSendCondition, WaSequenceStep

    ctx = await _scenario_claim(db_session)
    step1 = WaSequenceStep(id=str(uuid.uuid4()), campaign_id=ctx["campaign"].id,
                           step_index=1, template_a="Secondo messaggio.",
                           send_condition=WaSendCondition.always, wait_days=3)
    db_session.add(step1)
    ctx["cc"].current_step = 0
    ctx["cc"].status = WaContactStatus.in_sequence
    await db_session.commit()

    preso = await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1")
    assert preso is not None
    _, _, _, step = preso
    assert step.step_index == 1


@pytest.mark.asyncio
async def test_claim_ignora_contatto_oltre_soglia_fallimenti(db_session, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "wa_max_failures_per_contact", 3)
    ctx = await _scenario_claim(db_session)
    ctx["cc"].failure_count = 3
    await db_session.commit()
    assert await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1") is None


def _lock_profilo_libero(monkeypatch, renew_recorder: list | None = None):
    """Lucchetto profilo finto: `wa_profile_lock.held` farebbe un
    `arq.create_pool` VERO, e senza un demone Redis vivo l'attesa e' di ~50s
    (conn_retries=10 x conn_retry_delay=2) prima del fallimento -- un test di
    pura logica non deve dipendere da un'infrastruttura esterna. I test che
    provano DELIBERATAMENTE la mutua esclusione (fixture _redis_o_skip in
    test_wa_profile_lock.py) e il ramo WaProfileBusy restano com'erano."""
    import contextlib

    @contextlib.asynccontextmanager
    async def _libero(number_id, *, ttl_min=None):
        yield "token-di-test"
    monkeypatch.setattr(wa_worker.wa_profile_lock, "held", _libero)

    async def _renew(number_id, token, *, ttl_min=None):
        if renew_recorder is not None:
            renew_recorder.append((number_id, token))
        return True
    monkeypatch.setattr(wa_worker.wa_profile_lock, "renew", _renew)


async def _mini_sessione_con_doppi(db_session, monkeypatch, *, contatti=1,
                                   budget=True, ora_corrente=12,
                                   fake_invio=None, halted_getter=None,
                                   renew_recorder: list | None = None,
                                   orologio: dict | None = None,
                                   _ctx_out: dict | None = None):
    """Esercita esegui_mini_sessione con browser/POM/orologio finti.
    Il browser vero e' esercitato SOLO nel Task 15 (prova dal vivo): qui si
    prova la LOGICA, che e' dove hanno abitato tutti i difetti di M1.
    Anche il lucchetto profilo e' finto (vedi _lock_profilo_libero)."""
    import contextlib
    from app.config import settings
    from app.services import bot_state_service as bss, wa_number_manager as wnm
    from app.services.wa_sender import EsitoInvio

    monkeypatch.setattr(settings, "wa_send_enabled", True)
    orologio_virtuale(wa_worker, monkeypatch, orologio)
    _lock_profilo_libero(monkeypatch, renew_recorder)

    async def _halted(db=None):
        return halted_getter() if halted_getter else False
    monkeypatch.setattr(bss, "is_wa_halted", _halted)

    async def _budget(*a, **kw):
        return budget
    monkeypatch.setattr(wnm, "has_wa_send_budget", _budget)

    async def _cooldown_noop(*a, **kw):
        return None
    monkeypatch.setattr(wnm, "apply_wa_cooldown", _cooldown_noop)

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
async def test_g_fm2_singolo_no_existing_chat_non_arma_fm2(db_session, monkeypatch):
    """Drift SDD/contratto: il segnale 'nessun-messaggio-nel-pannello' ora
    produce skipped/no_existing_chat (guardia V2, non colpa nostra). UN
    contatto solo con questo esito NON deve fermare il numero: e' il caso
    normale, "un pannello lento su una cronologia vecchia"."""
    from app.services.wa_sender import EsitoInvio

    async def _no_existing_chat(*a, **kw):
        return EsitoInvio("skipped", "no_existing_chat")

    esito = await _mini_sessione_con_doppi(
        db_session, monkeypatch, fake_invio=_no_existing_chat, contatti=1)
    assert esito["motivo"] == "completata"   # un solo contatto, sessione finita normalmente
    assert esito["saltati"] == 1
    assert esito["inviati"] == 0


@pytest.mark.asyncio
async def test_g_fm2_cinque_no_existing_chat_consecutivi_fermano_il_numero(
        db_session, monkeypatch):
    """Rete di sicurezza richiesta esplicitamente: spostare il segnale fuori
    da FM2 non deve spegnere del tutto l'allarme su un DOM davvero rotto in
    quel punto. 5 'no_existing_chat' consecutivi armano comunque FM2, anche
    se il contatto stesso viene marcato skipped (non e' un guasto nostro sul
    singolo contatto, ma la SEQUENZA lo e')."""
    from app.services.wa_sender import EsitoInvio

    async def _no_existing_chat(*a, **kw):
        return EsitoInvio("skipped", "no_existing_chat")

    esito = await _mini_sessione_con_doppi(
        db_session, monkeypatch, fake_invio=_no_existing_chat, contatti=5)
    assert esito["motivo"] == "no_existing_chat_consecutivi"
    assert esito["inviati"] == 0
    assert esito["saltati"] == 5   # il contatto del 5o giro E' comunque skipped


@pytest.mark.asyncio
async def test_g_fm2_invio_riuscito_azzera_il_contatore_no_existing_chat(
        db_session, monkeypatch):
    """Il contatore e' CONSECUTIVO: un invio riuscito in mezzo lo azzera.
    4 no_existing_chat + 1 sent + altri 4 no_existing_chat (9 contatti in
    tutto) non devono armare FM2, perche' non ci sono mai 5 di fila."""
    from app.services.wa_sender import EsitoInvio

    chiamate = {"n": 0}

    async def _sequenza(*a, **kw):
        chiamate["n"] += 1
        if chiamate["n"] == 5:
            return EsitoInvio("sent", "ok")
        return EsitoInvio("skipped", "no_existing_chat")

    esito = await _mini_sessione_con_doppi(
        db_session, monkeypatch, fake_invio=_sequenza, contatti=9)
    assert esito["motivo"] == "completata"   # tutti e 9 processati, mai fermato
    assert esito["inviati"] == 1
    assert esito["saltati"] == 8


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


@pytest.mark.asyncio
async def test_fix_b_rollback_dopo_eccezione_evita_pendingrollbackerror(
        db_session, monkeypatch):
    """Fix B (review finale round 2, gap residuo su Fix 4/I7): quando
    l'eccezione IMPREVISTA da invia_a_contatto viene da un db.commit()
    fallito -- uno dei due trigger nominati nel finding originale, non un
    RuntimeError qualsiasi -- la AsyncSession resta genuinamente in stato
    'pending rollback'. Senza un rollback esplicito PRIMA di
    _rilascia_lock, la prima istruzione successiva che tocca db avrebbe
    sollevato PendingRollbackError FUORI da questo except, ripresentando
    I7 identico proprio per la classe di eccezione piu' citata.

    Qui si forza un commit fallito VERO (PK duplicata su wa_contacts, non
    un'eccezione inventata): e' lo stesso stato di sessione che lascerebbe
    un commit fallito dentro invia_a_contatto."""
    from sqlalchemy import select
    from app.models.wa import WaCampaignContact, WaContact

    async def _fake_invio_commit_fallito(db, pom, *, contact, **kw):
        dup = WaContact(id=contact.id, tenant_id=contact.tenant_id,
                        phone_hmac="dup-hmac", encrypted_phone="dup",
                        display_name="dup")
        db.add(dup)
        await db.commit()   # IntegrityError: PK duplicata -> pending-rollback

    ctx_out: dict = {}
    esito = await _mini_sessione_con_doppi(
        db_session, monkeypatch, fake_invio=_fake_invio_commit_fallito,
        contatti=1, _ctx_out=ctx_out)

    # Se PendingRollbackError fosse sfuggita da _rilascia_lock, non saremmo
    # arrivati qui: esegui_mini_sessione l'avrebbe propagata fuori. Con un
    # solo contatto (sotto MAX_GUASTI_CONSECUTIVI) la sessione chiude
    # regolarmente come 'completata', non come guasto.
    assert esito["motivo"] == "completata"

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
    rischedula con il break (minuti) via Retry(defer=...), non con un retry
    immediato.

    NON piu' _rischedula/enqueue_job (Fix A, review finale round 2): un
    enqueue_job con lo stesso _job_id chiamato da DENTRO il job ancora in
    esecuzione viene scartato in silenzio da ARQ (dedup sulla chiave
    arq:job:{job_id}, cancellata solo dopo il return della coroutine) -- il
    numero mandava una mini-sessione e basta. Retry evita enqueue_job del
    tutto: e' il worker ARQ a rimettere in coda lo stesso job dopo l'uscita."""
    from arq.worker import Retry

    async def _fake_mini(number_id):
        return {"inviati": 0, "falliti": 0, "saltati": 0, "motivo": "cap_esaurito"}
    monkeypatch.setattr(wa_worker, "esegui_mini_sessione", _fake_mini)

    async def _fake_campagna(number_id):
        return None
    monkeypatch.setattr(wa_worker, "_campagna_attiva_del_numero", _fake_campagna)
    monkeypatch.setattr(wa_worker.wa_timing, "wa_session_break_seconds", lambda c: 1234.0)

    with pytest.raises(Retry) as exc_info:
        await wa_worker.wa_send_task({}, "num-x")
    assert exc_info.value.defer_score == 1234000   # ms


@pytest.mark.asyncio
async def test_18b_wa_send_task_non_rischedula_su_motivi_terminali(db_session, monkeypatch):
    for motivo in ("send_disabled", "wa_halted", "numero_non_attivo",
                   "guasti_consecutivi", "niente_da_fare"):
        async def _fake_mini(number_id, _m=motivo):
            return {"motivo": _m}
        monkeypatch.setattr(wa_worker, "esegui_mini_sessione", _fake_mini)
        await wa_worker.wa_send_task({}, "num-x")   # non deve sollevare Retry


@pytest.mark.asyncio
async def test_20_recovery_avvio_chiude_i_sending_appesi(db_session):
    """QA item 20: nessuna chiamata al browser, i messaggi 'sending' vanno
    failed e il contatto legato a quel messaggio si ferma (skipped, non
    rieleggibile) -- decisione Tommaso round1: mai riprovare un invio di
    cui non si sa se e' partito davvero."""
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
                           locked_by="worker-crashato", locked_at=adesso_utc())
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
    assert cc.status == WaContactStatus.skipped
    assert cc.next_action_at is None

    await db_session.refresh(contact)
    assert contact.do_not_contact is False   # ambiguo per QUESTO tentativo, non DNC


@pytest.mark.asyncio
async def test_20b_recovery_non_tocca_contatti_senza_messaggio_appeso(db_session):
    """Il lock-release generico (per worker morti senza invio in corso) e'
    gia' compito del health-check periodico (cron_worker, timeout diverso).
    La recovery di avvio deve toccare SOLO i contatti legati a un
    wa_messages 'sending': un altro contatto lockato ma senza messaggio
    appeso deve restare esattamente com'era."""
    from app.models.tenant import Tenant
    from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                               WaCampaignType, WaContact, WaContactStatus, WaNumber)

    tenant = Tenant(id=str(uuid.uuid4()), name="T20b", status="active")
    db_session.add(tenant)
    await db_session.flush()
    contact = WaContact(id=str(uuid.uuid4()), tenant_id=tenant.id,
                        phone_hmac=f"h-{uuid.uuid4()}", encrypted_phone="e")
    number = WaNumber(id=str(uuid.uuid4()), tenant_id=tenant.id, label="n",
                      phone_hmac=f"h2-{uuid.uuid4()}", encrypted_phone="e")
    db_session.add_all([contact, number])
    await db_session.flush()
    campaign = WaCampaign(id=str(uuid.uuid4()), tenant_id=tenant.id, wa_number_id=number.id,
                          name="c20b", campaign_type=WaCampaignType.followup,
                          status=WaCampaignStatus.running)
    db_session.add(campaign)
    await db_session.flush()
    locked_at = adesso_utc()
    cc = WaCampaignContact(id=str(uuid.uuid4()), campaign_id=campaign.id, contact_id=contact.id,
                           status=WaContactStatus.queued, current_step=-1,
                           locked_by="worker-vivo-ma-lento", locked_at=locked_at)
    db_session.add(cc)
    await db_session.commit()

    n = await wa_worker.recover_wa_sending_on_startup()
    assert n == 0

    await db_session.refresh(cc)
    assert cc.locked_by == "worker-vivo-ma-lento"   # NON toccato
    assert cc.status == WaContactStatus.queued


@pytest.mark.asyncio
async def test_g5_recovery_non_declassa_contatto_gia_completato(db_session):
    """G5: la UPDATE su wa_campaign_contacts di recover_wa_sending_on_startup
    non filtrava per lo stato ATTUALE del contatto. Un contatto che ha
    lasciato un messaggio orfano 'sending' ma e' poi stato servito bene
    (completed) tornava 'skipped', corrompendo i conteggi della campagna e
    la sua chiusura automatica. Il messaggio orfano si chiude comunque
    (failed): e' il contatto che non deve muoversi."""
    from app.models.tenant import Tenant
    from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                               WaCampaignType, WaContact, WaContactStatus, WaMessage,
                               WaMessageStatus, WaNumber)

    tenant = Tenant(id=str(uuid.uuid4()), name="TG5a", status="active")
    db_session.add(tenant)
    await db_session.flush()
    contact = WaContact(id=str(uuid.uuid4()), tenant_id=tenant.id,
                        phone_hmac=f"h-{uuid.uuid4()}", encrypted_phone="e")
    number = WaNumber(id=str(uuid.uuid4()), tenant_id=tenant.id, label="n",
                      phone_hmac=f"h2-{uuid.uuid4()}", encrypted_phone="e")
    db_session.add_all([contact, number])
    await db_session.flush()
    campaign = WaCampaign(id=str(uuid.uuid4()), tenant_id=tenant.id, wa_number_id=number.id,
                          name="cg5a", campaign_type=WaCampaignType.followup,
                          status=WaCampaignStatus.running)
    db_session.add(campaign)
    await db_session.flush()
    cc = WaCampaignContact(id=str(uuid.uuid4()), campaign_id=campaign.id, contact_id=contact.id,
                           status=WaContactStatus.completed, current_step=0)
    msg = WaMessage(id=str(uuid.uuid4()), campaign_id=campaign.id, contact_id=contact.id,
                    wa_number_id=number.id, step_index=0, template_variant="a",
                    rendered_text="ciao", status=WaMessageStatus.sending)
    db_session.add_all([cc, msg])
    await db_session.commit()

    n = await wa_worker.recover_wa_sending_on_startup()
    assert n == 1   # il messaggio orfano si chiude comunque

    await db_session.refresh(msg)
    assert msg.status == WaMessageStatus.failed

    await db_session.refresh(cc)
    assert cc.status == WaContactStatus.completed   # NON declassato a skipped


@pytest.mark.asyncio
async def test_g5_recovery_declassa_ancora_contatto_in_sequence(db_session):
    """Non regressione: un contatto ancora IN LAVORAZIONE (in_sequence, non
    solo queued) con un messaggio orfano 'sending' va comunque fermato in
    skipped -- il fix G5 restringe il filtro dell'UPDATE a (queued,
    in_sequence), non lo rimuove."""
    from app.models.tenant import Tenant
    from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                               WaCampaignType, WaContact, WaContactStatus, WaMessage,
                               WaMessageStatus, WaNumber)

    tenant = Tenant(id=str(uuid.uuid4()), name="TG5b", status="active")
    db_session.add(tenant)
    await db_session.flush()
    contact = WaContact(id=str(uuid.uuid4()), tenant_id=tenant.id,
                        phone_hmac=f"h-{uuid.uuid4()}", encrypted_phone="e")
    number = WaNumber(id=str(uuid.uuid4()), tenant_id=tenant.id, label="n",
                      phone_hmac=f"h2-{uuid.uuid4()}", encrypted_phone="e")
    db_session.add_all([contact, number])
    await db_session.flush()
    campaign = WaCampaign(id=str(uuid.uuid4()), tenant_id=tenant.id, wa_number_id=number.id,
                          name="cg5b", campaign_type=WaCampaignType.followup,
                          status=WaCampaignStatus.running)
    db_session.add(campaign)
    await db_session.flush()
    cc = WaCampaignContact(id=str(uuid.uuid4()), campaign_id=campaign.id, contact_id=contact.id,
                           status=WaContactStatus.in_sequence, current_step=1,
                           locked_by="worker-crashato")
    msg = WaMessage(id=str(uuid.uuid4()), campaign_id=campaign.id, contact_id=contact.id,
                    wa_number_id=number.id, step_index=1, template_variant="a",
                    rendered_text="ciao", status=WaMessageStatus.sending)
    db_session.add_all([cc, msg])
    await db_session.commit()

    n = await wa_worker.recover_wa_sending_on_startup()
    assert n == 1

    await db_session.refresh(cc)
    assert cc.status == WaContactStatus.skipped
    assert cc.locked_by is None


@pytest.mark.asyncio
async def test_g5_recovery_e_guardia_doppio_invio_convergono(db_session):
    """La guardia anti-doppio-invio di PR #52 (wa_sender.py:317-352),
    trovando un WaMessage 'sending'/'sent' per la tripla (campagna, contatto,
    step), marca subito il contatto 'skipped' e LASCIA il messaggio in
    'sending'. Al riavvio successivo la recovery lo ritrova: con il filtro
    di stato del fix G5 (skipped non e' in (queued, in_sequence)) questo
    secondo passaggio deve diventare un no-op sul contatto, invece di
    ripetere la stessa scrittura sopra quella gia' fatta dalla guardia."""
    from app.models.tenant import Tenant
    from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                               WaCampaignType, WaContact, WaContactStatus, WaMessage,
                               WaMessageStatus, WaNumber)

    tenant = Tenant(id=str(uuid.uuid4()), name="TG5c", status="active")
    db_session.add(tenant)
    await db_session.flush()
    contact = WaContact(id=str(uuid.uuid4()), tenant_id=tenant.id,
                        phone_hmac=f"h-{uuid.uuid4()}", encrypted_phone="e")
    number = WaNumber(id=str(uuid.uuid4()), tenant_id=tenant.id, label="n",
                      phone_hmac=f"h2-{uuid.uuid4()}", encrypted_phone="e")
    db_session.add_all([contact, number])
    await db_session.flush()
    campaign = WaCampaign(id=str(uuid.uuid4()), tenant_id=tenant.id, wa_number_id=number.id,
                          name="cg5c", campaign_type=WaCampaignType.followup,
                          status=WaCampaignStatus.running)
    db_session.add(campaign)
    await db_session.flush()
    motivo_guardia = ("invio gia' registrato (sending) per questo step: "
                      "possibile guasto DB a invio avvenuto, verificare "
                      "se il messaggio e' arrivato")
    cc = WaCampaignContact(id=str(uuid.uuid4()), campaign_id=campaign.id, contact_id=contact.id,
                           status=WaContactStatus.skipped, current_step=0,
                           last_error=motivo_guardia)
    msg = WaMessage(id=str(uuid.uuid4()), campaign_id=campaign.id, contact_id=contact.id,
                    wa_number_id=number.id, step_index=0, template_variant="a",
                    rendered_text="ciao", status=WaMessageStatus.sending)
    db_session.add_all([cc, msg])
    await db_session.commit()

    n = await wa_worker.recover_wa_sending_on_startup()
    assert n == 1   # il messaggio si chiude comunque

    await db_session.refresh(msg)
    assert msg.status == WaMessageStatus.failed

    await db_session.refresh(cc)
    assert cc.status == WaContactStatus.skipped
    # il motivo scritto dalla guardia NON viene sovrascritto dal motivo
    # generico di recovery: il contatto non e' stato ri-toccato.
    assert cc.last_error == motivo_guardia


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
    _lock_profilo_libero(monkeypatch)
    # Questo test apre il browser senza passare da _mini_sessione_con_doppi,
    # quindi l'orologio virtuale se lo installa da solo: senza, l'attesa della
    # quarantena (M5.1) lo farebbe dormire un quarto d'ora davvero.
    orologio_virtuale(wa_worker, monkeypatch)

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
async def test_d20_due_enqueue_job_stesso_job_id_arq_scarta_il_duplicato(db_session, _redis_o_skip, monkeypatch):
    """Adversarial #20: un solo job attivo per numero -- niente doppia
    mini-sessione parallela sullo stesso numero (romperebbe il pacing).

    Il secondo enqueue scartato deve anche loggarsi: prima di questo fix
    tornava 'accodati:0' in silenzio, indistinguibile da un guasto vero
    (collaudo A3, 07/08 -- vedi docstring in enqueue_wa_workers). Spy
    diretto sul logger, non caplog: loguru non e' agganciato allo stdlib
    logging in questo repo, caplog non vedrebbe nulla."""
    import arq
    from app.services.work_enqueue import arq_redis_settings

    ctx = await _scenario_claim(db_session)
    await db_session.commit()

    messaggi = []
    monkeypatch.setattr(wa_worker.logger, "info", lambda msg, *a, **k: messaggi.append(msg))

    n1 = await wa_worker.enqueue_wa_workers(ctx["campaign"].id)
    n2 = await wa_worker.enqueue_wa_workers(ctx["campaign"].id)
    assert n1 == 1
    assert n2 == 0
    assert any("gia' schedulato" in m for m in messaggi)

    redis = await arq.create_pool(arq_redis_settings())
    try:
        job_id = wa_worker.wa_send_job_id(ctx["number"].id)
        await redis.zrem("arq:queue", job_id)
        await redis.delete(f"arq:job:{job_id}")
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_fix_a_retry_rischedula_dove_enqueue_job_verrebbe_scartato(
        db_session, monkeypatch, _redis_o_skip):
    """Fix A (review finale round 2), prova empirica contro Redis vero --
    non mock di _rischedula, che e' esattamente perche' il bug era passato
    inosservato nei test 18/18b prima di questo fix.

    Parte 1 riproduce il bug ORIGINALE per prova diretta: un enqueue_job
    manuale con lo stesso _job_id di un job la cui chiave arq:job:{job_id} e'
    ancora viva (qui simulata dal primo enqueue, che scrive quella chiave)
    torna None -- ARQ lo scarta in silenzio come duplicato, nessuna
    eccezione. E' esattamente quello che faceva _rischedula chiamata da
    dentro wa_send_task ancora in esecuzione (la chiave del job CORRENTE non
    e' ancora stata ripulita da finish_job, che gira solo dopo il return
    della coroutine).

    Parte 2 prova che il fix elimina il problema alla radice: si fa girare
    wa_send_task DAVVERO attraverso arq.worker.Worker.run_job (non la
    coroutine nuda chiamata a mano) sullo STESSO job_id/stessa chiave viva
    di sopra, con un motivo non terminale -- e si verifica via l'API di
    stato ARQ reale (Job.status()) che il job e' 'deferred' (rischedulato
    con score futuro, non fallito ne' droppato) e che la sua chiave
    arq:job:{job_id} e' ancora presente: la rischedulazione e' riuscita
    SENZA passare da enqueue_job, quindi il dedup della Parte 1 non si
    presenta mai."""
    import time
    import arq
    from arq.jobs import Job, JobStatus
    from arq.worker import Worker, func
    from app.services.work_enqueue import arq_redis_settings, ARQ_MAIN_QUEUE

    job_id = f"wa:send:test-fix-a-{uuid.uuid4().hex[:8]}"
    redis = await arq.create_pool(arq_redis_settings())
    try:
        # --- Parte 1: riproduce il bug originale ---
        job1 = await redis.enqueue_job("wa_send_task", "fake-number-id", _job_id=job_id)
        assert job1 is not None   # il primo enqueue riesce, scrive arq:job:{job_id}

        job2 = await redis.enqueue_job("wa_send_task", "fake-number-id", _job_id=job_id)
        assert job2 is None   # dedup silenzioso: ARQ non riaccoda, nessuna eccezione

        # --- Parte 2: il fix (Retry) rischedula lo STESSO job senza mai
        # chiamare enqueue_job, quindi il dedup di sopra non si presenta ---
        async def _fake_mini(number_id):
            return {"inviati": 1, "falliti": 0, "saltati": 0, "motivo": "cap_esaurito"}
        monkeypatch.setattr(wa_worker, "esegui_mini_sessione", _fake_mini)

        async def _fake_campagna(number_id):
            return None
        monkeypatch.setattr(wa_worker, "_campagna_attiva_del_numero", _fake_campagna)
        monkeypatch.setattr(wa_worker.wa_timing, "wa_session_break_seconds", lambda c: 5.0)

        worker = Worker(
            functions=[func(wa_worker.wa_send_task, name="wa_send_task", max_tries=10000)],
            redis_pool=redis,
            queue_name=ARQ_MAIN_QUEUE,
            handle_signals=False,
        )
        score = int(time.time() * 1000)
        await worker.run_job(job_id, score)

        status = await Job(job_id, redis, _queue_name=ARQ_MAIN_QUEUE).status()
        assert status == JobStatus.deferred   # rischedulato, non fallito/droppato

        ancora_viva = await redis.exists(f"arq:job:{job_id}")
        assert ancora_viva == 1   # la chiave dell'esecuzione corrente non e' stata cancellata
    finally:
        await redis.zrem(ARQ_MAIN_QUEUE, job_id)
        await redis.delete(f"arq:job:{job_id}", f"arq:result:{job_id}",
                           f"arq:in-progress:{job_id}", f"arq:retry:{job_id}")
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
async def test_mini_sessione_salta_se_profilo_occupato(db_session, monkeypatch):
    """M4 Task 2: il lock del profilo si controlla PRIMA di aprire il
    browser -- se un altro consumatore (health-check/reply-scan) ce l'ha
    gia', la mini-sessione esce con un motivo dedicato invece di litigare
    per lo stesso Chromium."""
    from app.config import settings
    from app.services import wa_profile_lock

    monkeypatch.setattr(settings, "wa_send_enabled", True)
    # Congelata dentro la finestra attiva (default 09:30-19:30): senza
    # questo il precheck "Cancello 2" (_niente_da_fare_prima_del_browser,
    # righe 208-211 di wa_worker.py) legge l'ora REALE di sistema e, fuori
    # dall'orario di lavoro, esce con "fuori_finestra" PRIMA di arrivare al
    # lucchetto del profilo che questo test vuole esercitare -- il test
    # falliva in CI/locale la sera (assert 'fuori_finestra' == 'profilo_
    # occupato'). Stesso valore/meccanismo del default ora_corrente=12 di
    # _mini_sessione_con_doppi piu' sotto in questo file.
    monkeypatch.setattr(wa_worker, "_ora_locale_corrente", lambda: 12)
    ctx = await _scenario_claim(db_session)

    class _CtxOccupato:
        def __call__(self, number_id, ttl_min=None):
            self._number_id = number_id
            return self

        async def __aenter__(self):
            raise wa_profile_lock.WaProfileBusy(self._number_id)

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(wa_worker.wa_profile_lock, "held", _CtxOccupato())

    esito = await wa_worker.esegui_mini_sessione(ctx["number"].id)
    assert esito["motivo"] == "profilo_occupato"
    assert esito["inviati"] == 0


@pytest.mark.asyncio
async def test_heartbeat_rinnova_il_lock_dopo_ogni_messaggio(db_session, monkeypatch):
    """M4 fixwave C4(a): il TTL del lucchetto va rinnovato mentre la sessione
    e' viva, altrimenti una sessione piu' lunga del TTL lascia il profilo
    aperto con il lock gia' scaduto -- e un secondo consumatore aprirebbe
    legittimamente un secondo Chromium sullo stesso profilo."""
    rinnovi = []
    esito = await _mini_sessione_con_doppi(db_session, monkeypatch, contatti=3,
                                           renew_recorder=rinnovi)
    assert esito["inviati"] == 3
    # Un rinnovo per messaggio, PIU' uno per ogni fetta dell'attesa di
    # quarantena (M5.1): l'attesa dura quanto il TTL non copre da sola, quindi
    # rinnova anche lei -- se non lo facesse, una sessione lunga si troverebbe
    # il lucchetto scaduto proprio mentre il browser e' ancora aperto.
    assert len(rinnovi) == fette_di_quarantena() + 3
    ctx_number_ids = {n for n, _ in rinnovi}
    assert len(ctx_number_ids) == 1
    assert all(token == "token-di-test" for _, token in rinnovi)


@pytest.mark.asyncio
async def test_cap_wall_clock_chiude_la_sessione_prima_del_ttl(db_session, monkeypatch):
    """M4 fixwave C4(c): oltre il cap la mini-sessione esce pulita con un
    motivo dedicato, invece di continuare confidando solo nel TTL."""
    from app.services.wa_sender import EsitoInvio

    # Orologio finto: ogni invio "costa" 100 minuti, quindi al secondo giro il
    # cap (ttl 90 - margine 5 = 85 min) e' gia' superato. Da M5.1 e' lo STESSO
    # orologio che l'helper usa per far scorrere l'attesa della quarantena
    # (15 min), altrimenti i due patch di perf_counter si sovrascriverebbero.
    orologio = {"t": 0.0}

    async def _invio_lentissimo(*a, **kw):
        orologio["t"] += 100 * 60
        return EsitoInvio("sent", "ok")

    from app.config import settings
    monkeypatch.setattr(settings, "wa_profile_lock_ttl_min", 90)

    esito = await _mini_sessione_con_doppi(db_session, monkeypatch, contatti=3,
                                           fake_invio=_invio_lentissimo,
                                           orologio=orologio)
    assert esito["motivo"] == "timeout_sessione"
    assert esito["inviati"] == 1


@pytest.mark.asyncio
async def test_d21_claim_confine_esatto_lock_timeout(db_session, monkeypatch):
    """Adversarial #21: 19min59s ancora fresco (rowcount=0), 20min01s stale
    (rowcount=1) -- nessuno scarto silenzioso di un secondo."""
    from app.config import settings
    monkeypatch.setattr(settings, "wa_lock_timeout_min", 20)

    ctx = await _scenario_claim(db_session)
    ctx["cc"].locked_by = "altro-worker"
    # AWARE per lo stesso motivo del test sul lock stale qui sopra.
    ctx["cc"].locked_at = adesso_utc() - timedelta(minutes=19, seconds=59)
    await db_session.commit()
    assert await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1") is None

    ctx["cc"].locked_at = adesso_utc() - timedelta(minutes=20, seconds=1)
    await db_session.commit()
    preso = await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w1")
    assert preso is not None and preso[0].locked_by == "w1"


class _PoolFinto:
    """Il minimo di ARQ che serve a enqueue_wa_workers: la coda e' uno zset
    finto, e enqueue_job torna None se il job_id c'e' gia' -- che e'
    esattamente il comportamento che ha causato l'incidente."""

    def __init__(self, gia_in_coda: dict | None = None):
        self.coda = dict(gia_in_coda or {})
        self.zadd_chiamate = []

    async def enqueue_job(self, _fn, *args, _job_id=None, **kw):
        if _job_id in self.coda:
            return None            # ARQ scarta il duplicato, in silenzio
        self.coda[_job_id] = 0
        return object()

    async def zscore(self, chiave, membro):
        return self.coda.get(membro)

    async def zadd(self, chiave, mapping):
        self.zadd_chiamate.append(mapping)
        self.coda.update(mapping)

    async def aclose(self):
        pass


def _monta_pool(monkeypatch, pool):
    async def _create_pool(*a, **kw):
        return pool
    monkeypatch.setattr(wa_worker.arq, "create_pool", _create_pool)


@pytest.mark.asyncio
async def test_riprendere_una_campagna_anticipa_il_job_gia_schedulato(
        db_session, monkeypatch):
    """Il 12/08 Tommaso ha spento il bot a campagna running, l'ha rimessa in
    pausa e ripresa: la UI diceva 'running' e non partiva NIENTE.

    Il job di invio ha un _job_id fisso per numero (`wa:send:<id>`), e ARQ
    scarta in silenzio un enqueue con un id gia' presente. Il job differito
    dal break anti-ban era in coda con lo score a +16 minuti: `riprendi()`
    ristampava next_action_at sulle righe, l'enqueue veniva scartato, e il
    worker restava addormentato fino allo score vecchio. Nessun errore da
    nessuna parte -- lo stato a schermo diceva 'In corso'.

    Chi riprende a mano deve poter ripartire. Il job in coda si ANTICIPA
    invece di scartarlo, senza mai duplicarlo (l'id resta uno)."""
    import time
    from app.models.wa import WaCampaignStatus

    ctx = await _scenario_claim(db_session)
    ctx["campaign"].status = WaCampaignStatus.running
    await db_session.commit()

    fra_16_minuti = time.time() * 1000 + 16 * 60 * 1000
    job_id = wa_worker.wa_send_job_id(ctx["number"].id)
    pool = _PoolFinto({job_id: fra_16_minuti})
    _monta_pool(monkeypatch, pool)

    n = await wa_worker.enqueue_wa_workers(ctx["campaign"].id,
                                           anticipa_se_differito=True)

    assert n == 1, "l'utente ha ripreso e non gli e' stato accodato nulla"
    assert pool.zadd_chiamate, "il job non e' stato anticipato"
    assert len(pool.coda) == 1, "il job e' stato duplicato invece che anticipato"
    nuovo = pool.coda[job_id]
    assert nuovo < fra_16_minuti, "lo score non e' stato anticipato"


@pytest.mark.asyncio
async def test_anticipare_non_azzera_la_pausa_anti_ban(db_session, monkeypatch):
    """Il contrappeso, e il motivo per cui non si anticipa a 'adesso'.

    Il break fra mini-sessioni e' una protezione anti-ban, non un ritardo da
    saltare. Ripartire a tempo zero manderebbe il messaggio successivo
    attaccato al precedente -- esattamente la firma che WhatsApp cerca, e la
    stessa famiglia di errore delle due protezioni abbassate insieme il 09/08.

    Si taglia l'attesa lunga, non il delay fra due invii.

    Il delay e' fissato a mano di proposito: e' lognormale, e un'asserzione
    tipo `score > adesso` passerebbe anche con l'anticipo a tempo zero, per i
    microsecondi che passano fra la lettura dell'orologio nel test e quella
    dentro la funzione. Un test cosi' non prova niente -- verificato
    rimettendo il difetto."""
    import time
    from app.models.wa import WaCampaignStatus
    from app.services import wa_timing

    ctx = await _scenario_claim(db_session)
    ctx["campaign"].status = WaCampaignStatus.running
    await db_session.commit()

    monkeypatch.setattr(wa_timing, "wa_send_delay_seconds", lambda: 30.0)

    ora = time.time() * 1000
    job_id = wa_worker.wa_send_job_id(ctx["number"].id)
    pool = _PoolFinto({job_id: ora + 16 * 60 * 1000})
    _monta_pool(monkeypatch, pool)

    await wa_worker.enqueue_wa_workers(ctx["campaign"].id,
                                       anticipa_se_differito=True)

    attesa_s = (pool.coda[job_id] - ora) / 1000
    assert attesa_s >= 29, (
        f"riparte fra {attesa_s:.1f}s: salta il delay anti-ban di 30s")


@pytest.mark.asyncio
async def test_il_supervisore_automatico_non_anticipa_niente(db_session, monkeypatch):
    """Il cron che riaccoda le campagne running NON deve anticipare: li' un
    job differito e' differito per una ragione (cap esaurito, fuori finestra,
    break di sessione), e anticiparlo a ogni giro annullerebbe il pacing.

    Solo un'azione umana esplicita -- avvia, riprendi, kick -- sposta lo
    score. Per questo il default e' False."""
    import time
    from app.models.wa import WaCampaignStatus

    ctx = await _scenario_claim(db_session)
    ctx["campaign"].status = WaCampaignStatus.running
    await db_session.commit()

    fra_16_minuti = time.time() * 1000 + 16 * 60 * 1000
    job_id = wa_worker.wa_send_job_id(ctx["number"].id)
    pool = _PoolFinto({job_id: fra_16_minuti})
    _monta_pool(monkeypatch, pool)

    await wa_worker.enqueue_wa_workers(ctx["campaign"].id)   # default

    assert not pool.zadd_chiamate, "il flusso automatico ha spostato lo score"
    assert pool.coda[job_id] == fra_16_minuti
