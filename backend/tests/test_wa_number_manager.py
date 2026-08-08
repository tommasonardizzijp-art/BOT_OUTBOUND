from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.services import wa_number_manager as wnm
from app.models.wa import WaNumberStatus


def _number(**over):
    base = dict(daily_cap=100, warmup_day=0, sent_today=0, sent_date=None,
                status=WaNumberStatus.active)
    base.update(over)
    return SimpleNamespace(**base)


def _campaign(daily_limit=None):
    return SimpleNamespace(daily_limit=daily_limit)


def test_effective_cap_uses_warmup_step_when_warming(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "wa_warmup_steps", "20,20,30,40,60,80,100")
    monkeypatch.setattr(settings, "wa_daily_cap_default", 20)
    number = _number(daily_cap=200, warmup_day=3)  # 3o valore della lista = 30
    assert wnm.effective_wa_daily_cap(number, _campaign()) == 30


def test_effective_cap_past_warmup_uses_last_step(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "wa_warmup_steps", "20,20,30,40,60,80,100")
    number = _number(daily_cap=200, warmup_day=99)
    assert wnm.effective_wa_daily_cap(number, _campaign()) == 100


def test_effective_cap_ignora_il_gradino_se_la_rampa_e_disabilitata(monkeypatch):
    """G4 (flag, 08/08): con wa_warmup_enabled=False il gradino di warmup
    non entra piu' nel min(), QUALUNQUE sia warmup_day -- anche se la riga
    dice 'sono al giorno 3' (gradino 30), il tetto resta il daily_cap nudo
    del numero."""
    from app.config import settings
    monkeypatch.setattr(settings, "wa_warmup_steps", "20,20,30,40,60,80,100")
    monkeypatch.setattr(settings, "wa_warmup_enabled", False)
    number = _number(daily_cap=200, warmup_day=3)   # gradino 3 = 30, ignorato
    assert wnm.effective_wa_daily_cap(number, _campaign()) == 200


def test_effective_cap_flag_true_di_default_comportamento_invariato(monkeypatch):
    """Non-regressione esplicita: con wa_warmup_enabled=True (default, non
    toccato) il gradino torna a comandare come prima del flag."""
    from app.config import settings
    monkeypatch.setattr(settings, "wa_warmup_steps", "20,20,30,40,60,80,100")
    assert settings.wa_warmup_enabled is True   # non toccato, e' il default
    number = _number(daily_cap=200, warmup_day=3)
    assert wnm.effective_wa_daily_cap(number, _campaign()) == 30


def test_effective_cap_is_min_of_number_campaign_and_warmup(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "wa_warmup_steps", "20,20,30,40,60,80,100")
    number = _number(daily_cap=200, warmup_day=7)   # step 7 = 100
    campaign = _campaign(daily_limit=15)
    assert wnm.effective_wa_daily_cap(number, campaign) == 15


def test_wa_sent_today_resets_on_new_day():
    number = _number(sent_today=45, sent_date="2000-01-01")
    assert wnm.wa_sent_today(number) == 0


def test_wa_sent_today_keeps_count_same_day():
    today = datetime.utcnow().strftime("%Y-%m-%d")
    number = _number(sent_today=7, sent_date=today)
    assert wnm.wa_sent_today(number) == 7


@pytest.mark.asyncio
async def test_record_wa_sent_atomic_increment_with_rollover(db_session):
    from app.models.tenant import Tenant
    from app.models.wa import WaNumber
    import uuid

    tenant = Tenant(id=str(uuid.uuid4()), name="T", status="active")
    db_session.add(tenant)
    await db_session.flush()
    number = WaNumber(
        id=str(uuid.uuid4()), tenant_id=tenant.id, label="n", phone_hmac="h1",
        encrypted_phone="e1", daily_cap=100, warmup_day=0, sent_today=9,
        sent_date="2000-01-01",
    )
    db_session.add(number)
    await db_session.commit()

    await wnm.record_wa_sent(db_session, number.id)
    await db_session.refresh(number)
    assert number.sent_today == 1  # era di ieri: riparte da 1, non 10
    assert number.sent_date == datetime.utcnow().strftime("%Y-%m-%d")

    await wnm.record_wa_sent(db_session, number.id)
    await db_session.refresh(number)
    assert number.sent_today == 2  # stesso giorno: incrementa


@pytest.mark.asyncio
async def test_apply_and_release_wa_cooldown(monkeypatch):
    calls = {}

    class _FakeRedis:
        async def set(self, key, value, ex=None):
            calls["set"] = (key, value, ex)
        async def exists(self, key):
            return calls.get("exists_result", 0)
        async def aclose(self):
            pass

    async def _fake_pool(*a, **kw):
        return _FakeRedis()

    monkeypatch.setattr(wnm.arq, "create_pool", _fake_pool)
    await wnm.apply_wa_cooldown("num-1", minutes=30)
    assert calls["set"][0] == "wa:cooldown:num-1"
    assert calls["set"][2] == 30 * 60

    calls["exists_result"] = 0  # TTL scaduto in Redis
    released = await wnm.release_expired_wa_cooldowns()
    assert released == []  # nessun numero passato: serve la query DB (step successivo)


@pytest_asyncio.fixture
async def _tenant(db_session):
    import uuid
    from app.models.tenant import Tenant
    t = Tenant(id=str(uuid.uuid4()), name="T-numman", status="active")
    db_session.add(t)
    await db_session.commit()
    return t


@pytest.mark.asyncio
async def test_16_cap_globale_di_un_altro_numero_blocca_anche_chi_ha_margine(
        db_session, monkeypatch, _tenant):
    """QA item 16 (funzionale) + adversarial #23 (confine esatto)."""
    import uuid
    from app.config import settings
    from app.models.wa import WaNumber

    monkeypatch.setattr(settings, "wa_global_daily_cap", 5)
    oggi = wnm._utc_today_str()

    numero_b = WaNumber(id=str(uuid.uuid4()), tenant_id=_tenant.id, label="B",
                        phone_hmac=f"h-{uuid.uuid4()}", encrypted_phone="e",
                        daily_cap=20, warmup_day=0, sent_today=5, sent_date=oggi)
    numero_a = WaNumber(id=str(uuid.uuid4()), tenant_id=_tenant.id, label="A",
                        phone_hmac=f"h-{uuid.uuid4()}", encrypted_phone="e",
                        daily_cap=20, warmup_day=0, sent_today=0, sent_date=oggi)
    db_session.add_all([numero_b, numero_a])
    await db_session.commit()

    campagna = SimpleNamespace(daily_limit=None)
    ok = await wnm.has_wa_send_budget(db_session, numero_a, campagna)
    assert ok is False   # margine individuale di A non basta: il globale e' saturo


@pytest.mark.asyncio
async def test_22_cap_esatto_del_numero_blocca(db_session, monkeypatch, _tenant):
    """Adversarial #22: sent_today == daily_cap -> nessun budget (>=, non >)."""
    import uuid
    from app.config import settings
    from app.models.wa import WaNumber

    monkeypatch.setattr(settings, "wa_global_daily_cap", 999)
    oggi = wnm._utc_today_str()
    numero = WaNumber(id=str(uuid.uuid4()), tenant_id=_tenant.id, label="n",
                      phone_hmac=f"h-{uuid.uuid4()}", encrypted_phone="e",
                      daily_cap=5, warmup_day=0, sent_today=5, sent_date=oggi)
    db_session.add(numero)
    await db_session.commit()

    ok = await wnm.has_wa_send_budget(db_session, numero, SimpleNamespace(daily_limit=None))
    assert ok is False


@pytest_asyncio.fixture
async def _tenant_warmup(db_session):
    import uuid
    from app.models.tenant import Tenant
    t = Tenant(id=str(uuid.uuid4()), name="T-warmup", status="active")
    db_session.add(t)
    await db_session.commit()
    return t


def _make_wa_number(tenant_id, **over):
    """Helper locale (non factories_wa.make_number): serve controllare
    warmup_advanced_date/status/warmup_day riga per riga in ogni test."""
    import uuid
    from app.models.wa import WaNumber

    base = dict(
        id=str(uuid.uuid4()), tenant_id=tenant_id, label="n",
        phone_hmac=f"h-{uuid.uuid4()}", encrypted_phone="e",
        daily_cap=100, warmup_day=1, warmup_advanced_date=None,
        status=WaNumberStatus.active,
    )
    base.update(over)
    return WaNumber(**base)


@pytest.mark.asyncio
async def test_la_rampa_sale_un_gradino_al_giorno_in_MESSAGGI(
        db_session, monkeypatch, _tenant_warmup):
    """IL test della rampa, e l'unico che avrebbe intercettato il bug di M5.

    Asserisce sui MESSAGGI AL GIORNO, non sull'indice: `warmup_day` e' un
    indice nella lista dei gradini, quindi un test che guarda solo il suo
    valore non distingue "la rampa sale" da "la rampa e' saltata". La prima
    stesura di questi test asseriva `warmup_day == 7` dopo un avanzamento da 1
    e passava, mentre il numero era gia' al cap massimo dal secondo giorno.

    Nessuna delle assert qui sotto guarda l'indice: guardano il tetto in
    messaggi, che e' la cosa che fa bannare un numero se cresce troppo in
    fretta.
    """
    from app.config import settings
    monkeypatch.setattr(settings, "wa_warmup_steps", "20,20,30,40,60,80,100")
    monkeypatch.setattr(settings, "wa_warmup_advance_steps_per_day", 1)

    numero = _make_wa_number(_tenant_warmup.id, warmup_day=1, daily_cap=1000)
    db_session.add(numero)
    await db_session.commit()
    campagna = type("C", (), {"daily_limit": None})()

    # daily_cap volutamente altissimo: qui deve comandare SOLO il gradino,
    # altrimenti il min() lo maschera ed e' il motivo per cui il bug era
    # rimasto invisibile nell'uso normale.
    attesi = [20, 20, 30, 40, 60, 80, 100, 100, 100]
    ottenuti = [wnm.effective_wa_daily_cap(numero, campagna)]

    for _ in range(len(attesi) - 1):
        numero.warmup_advanced_date = None  # simula "e' passato un giorno"
        await db_session.commit()
        await wnm.advance_wa_warmup_if_needed()
        await db_session.refresh(numero)
        ottenuti.append(wnm.effective_wa_daily_cap(numero, campagna))

    assert ottenuti == attesi, (
        f"la rampa in messaggi/giorno e' {ottenuti}, attesa {attesi}")

    # E nessun giorno deve salire piu' del gradino successivo previsto: e'
    # questa la proprieta' che protegge il numero, non il valore finale.
    salti = [b - a for a, b in zip(ottenuti, ottenuti[1:])]
    assert max(salti) <= 20, f"salto troppo grande fra due giorni: {salti}"


def test_la_configurazione_DI_DEFAULT_produce_una_rampa_graduale():
    """Gli altri test della rampa usano monkeypatch, quindi passerebbero anche
    se qualcuno rimettesse un passo sbagliato nel default di config.py -- ed e'
    esattamente cosi' che il bug di M5 e' arrivato fino al collaudo. Questo
    test NON monkeypatcha niente: legge i valori veri con cui l'applicazione
    parte, e fallisce se la rampa reale non e' graduale.

    Non impone QUALI debbano essere i gradini (e' una decisione di prodotto,
    si cambiano liberamente): impone che, qualunque siano, il tetto in
    messaggi non raddoppi da un giorno all'altro e che servano piu' giorni per
    arrivare a regime.
    """
    from app.config import settings

    steps = wnm._parse_wa_warmup_steps(settings.wa_warmup_steps)
    assert len(steps) >= 3, "una rampa di meno di 3 gradini non e' una rampa"

    passo = settings.wa_warmup_advance_steps_per_day
    assert passo >= 1, "il passo e' in gradini/giorno: sotto 1 la rampa non sale"

    # Simula i primi giorni di vita di un numero nuovo, coi valori veri.
    giorno, caps = 1, []
    for _ in range(len(steps) + 2):
        caps.append(wnm.get_wa_warmup_cap(giorno))
        giorno = min(giorno + passo, len(steps))

    assert caps[0] == steps[0], "il giorno 1 deve partire dal primo gradino"

    salti = [b - a for a, b in zip(caps, caps[1:])]
    salto_massimo_previsto = max(
        b - a for a, b in zip(steps, steps[1:])) if len(steps) > 1 else 0
    assert max(salti) <= salto_massimo_previsto, (
        f"la rampa reale salta {max(salti)} messaggi fra due giorni "
        f"consecutivi, ma il salto piu' grande fra due gradini adiacenti e' "
        f"{salto_massimo_previsto}: il passo "
        f"(WA_WARMUP_ADVANCE_STEPS_PER_DAY={passo}) sta scavalcando gradini. "
        f"Rampa risultante: {caps}"
    )

    giorni_per_arrivare_a_regime = caps.index(max(caps)) + 1
    assert giorni_per_arrivare_a_regime >= len(steps), (
        f"il cap massimo viene raggiunto al giorno "
        f"{giorni_per_arrivare_a_regime} su {len(steps)} gradini: la rampa "
        f"esiste per distribuire la crescita nel tempo, non per arrivarci "
        f"subito. Rampa risultante: {caps}")


@pytest.mark.asyncio
async def test_advance_wa_warmup_avanza_di_un_solo_gradino(
        db_session, monkeypatch, _tenant_warmup):
    from app.config import settings
    monkeypatch.setattr(settings, "wa_warmup_steps", "20,20,30,40,60,80,100")
    monkeypatch.setattr(settings, "wa_warmup_advance_steps_per_day", 1)

    numero = _make_wa_number(_tenant_warmup.id, warmup_day=1)
    db_session.add(numero)
    await db_session.commit()

    await wnm.advance_wa_warmup_if_needed()

    await db_session.refresh(numero)
    assert numero.warmup_day == 2
    assert numero.warmup_advanced_date == wnm._utc_today_str()


@pytest.mark.asyncio
async def test_advance_wa_warmup_non_avanza_nessun_numero_se_la_rampa_e_disabilitata(
        db_session, monkeypatch, _tenant_warmup):
    """G4 (flag, 08/08): a wa_warmup_enabled=False, advance_wa_warmup_if_needed
    deve essere un no-op totale -- nessun numero avanza, nessuno warmup_
    advanced_date viene toccato. Stesso principio del guard esistente su
    WA_WARMUP_ADVANCE_STEPS_PER_DAY<=0 (routine gia' in questa funzione),
    ma sul flag invece che sul passo."""
    from app.config import settings
    monkeypatch.setattr(settings, "wa_warmup_steps", "20,20,30,40,60,80,100")
    monkeypatch.setattr(settings, "wa_warmup_enabled", False)

    numero = _make_wa_number(_tenant_warmup.id, warmup_day=1, warmup_advanced_date=None)
    db_session.add(numero)
    await db_session.commit()

    await wnm.advance_wa_warmup_if_needed()

    await db_session.refresh(numero)
    assert numero.warmup_day == 1
    assert numero.warmup_advanced_date is None


@pytest.mark.asyncio
async def test_il_passo_e_in_GRADINI_non_in_messaggi(
        db_session, monkeypatch, _tenant_warmup):
    """Guardia sull'unita' di misura del passo, che e' l'errore da cui nasce
    tutto: con una lista lunga e un passo di 3, l'indice deve salire di 3
    POSIZIONI (non di 3 messaggi, e non di 3 x qualcosa)."""
    from app.config import settings
    monkeypatch.setattr(settings, "wa_warmup_steps",
                        "20,20,30,40,50,60,70,80,90,100,110,120,130,140,150")
    monkeypatch.setattr(settings, "wa_warmup_advance_steps_per_day", 3)

    numero = _make_wa_number(_tenant_warmup.id, warmup_day=3)
    db_session.add(numero)
    await db_session.commit()

    await wnm.advance_wa_warmup_if_needed()

    await db_session.refresh(numero)
    assert numero.warmup_day == 6  # 3 + 3 posizioni, lontano dal clamp
    # 6o gradino della lista = 60 msg/giorno
    assert wnm.get_wa_warmup_cap(numero.warmup_day) == 60


@pytest.mark.asyncio
async def test_advance_wa_warmup_e_idempotente_stesso_giorno(
        db_session, monkeypatch, _tenant_warmup):
    from app.config import settings
    monkeypatch.setattr(settings, "wa_warmup_steps", "20,20,30,40,60,80,100")

    numero = _make_wa_number(
        _tenant_warmup.id, warmup_day=2, warmup_advanced_date=wnm._utc_today_str())
    db_session.add(numero)
    await db_session.commit()

    await wnm.advance_wa_warmup_if_needed()

    await db_session.refresh(numero)
    assert numero.warmup_day == 2  # gia' avanzato oggi: nessun secondo incremento


@pytest.mark.asyncio
async def test_advance_wa_warmup_non_supera_ultimo_gradino(
        db_session, monkeypatch, _tenant_warmup):
    from app.config import settings
    steps = "20,20,30,40,60,80,100"
    monkeypatch.setattr(settings, "wa_warmup_steps", steps)
    ultimo = len(steps.split(","))  # 7

    numero = _make_wa_number(_tenant_warmup.id, warmup_day=ultimo)
    db_session.add(numero)
    await db_session.commit()

    await wnm.advance_wa_warmup_if_needed()

    await db_session.refresh(numero)
    assert numero.warmup_day == ultimo  # a regime: nessun avanzamento oltre


@pytest.mark.asyncio
@pytest.mark.parametrize("stato", [WaNumberStatus.suspended, WaNumberStatus.cooldown,
                                    WaNumberStatus.disconnected, WaNumberStatus.retired,
                                    WaNumberStatus.pending_qr, WaNumberStatus.qr_required])
async def test_advance_wa_warmup_ignora_numeri_non_active(
        db_session, monkeypatch, _tenant_warmup, stato):
    from app.config import settings
    monkeypatch.setattr(settings, "wa_warmup_steps", "20,20,30,40,60,80,100")

    numero = _make_wa_number(_tenant_warmup.id, warmup_day=1, status=stato)
    db_session.add(numero)
    await db_session.commit()

    await wnm.advance_wa_warmup_if_needed()

    await db_session.refresh(numero)
    assert numero.warmup_day == 1


@pytest.mark.asyncio
async def test_advance_wa_warmup_override_manuale_oggi_non_blocca_avanzamento_di_domani(
        db_session, monkeypatch, _tenant_warmup):
    """Un override manuale (PATCH warmup_day) non tocca warmup_advanced_date:
    la volta successiva che il cron/boot chiama advance_wa_warmup_if_needed
    (di fatto 'domani', qui simulato con la guardia None/non-oggi) il numero
    avanza normalmente, a prescindere dall'override."""
    from app.config import settings
    monkeypatch.setattr(settings, "wa_warmup_steps", "20,20,30,40,60,80,100")
    monkeypatch.setattr(settings, "wa_warmup_advance_steps_per_day", 1)

    # Override manuale: warmup_day portato a 5, warmup_advanced_date MAI toccato.
    numero = _make_wa_number(_tenant_warmup.id, warmup_day=5, warmup_advanced_date=None)
    db_session.add(numero)
    await db_session.commit()

    await wnm.advance_wa_warmup_if_needed()

    await db_session.refresh(numero)
    # avanza comunque (l'override non lo blocca): 5 -> 6, un gradino
    assert numero.warmup_day == 6
    assert numero.warmup_advanced_date == wnm._utc_today_str()


@pytest.mark.asyncio
async def test_adv23_cap_globale_esattamente_raggiunto_non_superato(
        db_session, monkeypatch, _tenant):
    """Adversarial #23: global_sent == WA_GLOBAL_DAILY_CAP (non oltre) blocca
    comunque -- il confronto e' `<`, non `<=`."""
    import uuid
    from app.config import settings
    from app.models.wa import WaNumber

    monkeypatch.setattr(settings, "wa_global_daily_cap", 10)
    oggi = wnm._utc_today_str()
    numero_b = WaNumber(id=str(uuid.uuid4()), tenant_id=_tenant.id, label="B",
                        phone_hmac=f"h-{uuid.uuid4()}", encrypted_phone="e",
                        daily_cap=20, warmup_day=0, sent_today=10, sent_date=oggi)
    numero_a = WaNumber(id=str(uuid.uuid4()), tenant_id=_tenant.id, label="A",
                        phone_hmac=f"h-{uuid.uuid4()}", encrypted_phone="e",
                        daily_cap=20, warmup_day=0, sent_today=0, sent_date=oggi)
    db_session.add_all([numero_b, numero_a])
    await db_session.commit()

    ok = await wnm.has_wa_send_budget(db_session, numero_a, SimpleNamespace(daily_limit=None))
    assert ok is False


@pytest.mark.asyncio
async def test_config_warmup_steps_malformata_non_uccide_il_boot(
        db_session, monkeypatch, _tenant_warmup):
    """_parse_wa_warmup_steps gira nel lifespan del boot E nel calcolo del cap
    di ogni invio: un refuso in WA_WARMUP_STEPS faceva morire l'avvio
    dell'applicazione con un ValueError grezzo. Deve degradare, non esplodere.
    Trovato nel collaudo M5."""
    from app.config import settings

    monkeypatch.setattr(settings, "wa_warmup_steps", "20,abc,40")
    numero = _make_wa_number(_tenant_warmup.id, warmup_day=1)
    db_session.add(numero)
    await db_session.commit()

    await wnm.advance_wa_warmup_if_needed()  # non deve sollevare

    # La voce sporca e' scartata, i gradini validi restano usabili.
    assert wnm._parse_wa_warmup_steps("20,abc,40") == [20, 40]
    assert isinstance(wnm.get_wa_warmup_cap(1), int)


@pytest.mark.asyncio
async def test_incremento_negativo_in_config_non_fa_retrocedere_la_rampa(
        db_session, monkeypatch, _tenant_warmup):
    """Un WA_WARMUP_ADVANCE_STEPS_PER_DAY negativo portava warmup_day sotto zero, e
    sotto zero il gradino esce dal min() di effective_wa_daily_cap: una
    configurazione sbagliata RIMUOVEVA il tetto anti-ban invece di rallentare
    la rampa, per giunta in modo definitivo (il numero usciva per sempre dallo
    sweep). Questo contatore ha una direzione sola."""
    from app.config import settings

    monkeypatch.setattr(settings, "wa_warmup_steps", "20,20,30,40,60,80,100")
    monkeypatch.setattr(settings, "wa_warmup_advance_steps_per_day", -5)

    numero = _make_wa_number(_tenant_warmup.id, warmup_day=2)
    db_session.add(numero)
    await db_session.commit()

    await wnm.advance_wa_warmup_if_needed()

    await db_session.refresh(numero)
    assert numero.warmup_day == 2, (
        "con un passo negativo l'avanzamento va SOSPESO, non invertito e "
        "nemmeno forzato a salire: la configurazione e' sbagliata e va "
        "corretta, non interpretata")

    # E il tetto resta calcolabile e attivo (non "nessun tetto").
    campagna = type("C", (), {"daily_limit": None})()
    numero.daily_cap = 1000
    assert wnm.effective_wa_daily_cap(numero, campagna) <= 100


@pytest.mark.asyncio
async def test_passo_zero_congela_la_rampa_invece_di_farla_salire(
        db_session, monkeypatch, _tenant_warmup):
    """Chi mette 0 sta chiedendo di congelare la rampa: la richiesta va
    onorata fermandosi, non forzando il passo a 1.

    Il primo fix del passo negativo usava `max(1, passo)` e finiva per far
    salire la rampa anche a 0, mentre `warmup_advanced_date` continuava ad
    aggiornarsi: sembrava funzionare e faceva l'opposto di quanto chiesto.
    Trovato nella review finale di M5.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "wa_warmup_steps", "20,20,30,40,60,80,100")
    monkeypatch.setattr(settings, "wa_warmup_advance_steps_per_day", 0)

    numero = _make_wa_number(_tenant_warmup.id, warmup_day=2, warmup_advanced_date=None)
    db_session.add(numero)
    await db_session.commit()

    await wnm.advance_wa_warmup_if_needed()

    await db_session.refresh(numero)
    assert numero.warmup_day == 2, "con passo 0 la rampa non deve salire"
    # E nemmeno la guardia va toccata: un giorno "saltato" non deve risultare
    # come un giorno gia' avanzato, altrimenti rimettere il passo a 1 domani
    # non recupererebbe nulla.
    assert numero.warmup_advanced_date is None
