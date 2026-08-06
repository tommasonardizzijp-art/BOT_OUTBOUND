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
async def test_advance_wa_warmup_avanza_numero_active_non_ancora_avanzato_oggi(
        db_session, monkeypatch, _tenant_warmup):
    from app.config import settings
    monkeypatch.setattr(settings, "wa_warmup_steps", "20,20,30,40,60,80,100")
    monkeypatch.setattr(settings, "wa_warmup_advance_per_day", 10)

    numero = _make_wa_number(_tenant_warmup.id, warmup_day=1)
    db_session.add(numero)
    await db_session.commit()

    await wnm.advance_wa_warmup_if_needed()

    await db_session.refresh(numero)
    # +10/giorno (decisione 06/08), clampato all'ultimo gradino (7): 1+10 -> 7
    assert numero.warmup_day == 7
    assert numero.warmup_advanced_date == wnm._utc_today_str()


@pytest.mark.asyncio
async def test_advance_wa_warmup_incrementa_esattamente_di_10_senza_clamp(
        db_session, monkeypatch, _tenant_warmup):
    """Steps abbastanza lunga da non saturare al primo salto: verifica
    l'incremento REALE (+10), non solo il risultato clampato all'ultimo
    gradino che da solo non distinguerebbe +10 da un ipotetico bug a +1."""
    from app.config import settings
    monkeypatch.setattr(settings, "wa_warmup_steps",
                        "20,20,30,40,50,60,70,80,90,100,110,120,130,140,150")
    monkeypatch.setattr(settings, "wa_warmup_advance_per_day", 10)

    numero = _make_wa_number(_tenant_warmup.id, warmup_day=3)
    db_session.add(numero)
    await db_session.commit()

    await wnm.advance_wa_warmup_if_needed()

    await db_session.refresh(numero)
    assert numero.warmup_day == 13  # 3 + 10, ben sotto i 15 gradini: nessun clamp in gioco


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
    monkeypatch.setattr(settings, "wa_warmup_advance_per_day", 10)

    # Override manuale: warmup_day portato a 5, warmup_advanced_date MAI toccato.
    numero = _make_wa_number(_tenant_warmup.id, warmup_day=5, warmup_advanced_date=None)
    db_session.add(numero)
    await db_session.commit()

    await wnm.advance_wa_warmup_if_needed()

    await db_session.refresh(numero)
    # avanza comunque (l'override non lo blocca), clampato all'ultimo gradino: 5+10 -> 7
    assert numero.warmup_day == 7
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
