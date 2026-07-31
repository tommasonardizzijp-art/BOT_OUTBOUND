from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

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
