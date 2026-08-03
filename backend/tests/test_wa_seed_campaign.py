"""QA M3 gruppo A (test 1-5): script di seed scripts/wa_seed_campaign.py.
Nessun test dedicato esisteva per questo script prima della QA di fine
modulo (Task 15 Step 3, qa-m3-tests.md)."""
import sys
import uuid

import pytest
from sqlalchemy import select

from scripts import wa_seed_campaign as seed


def _argv(tenant, number_label, phone, contact, camp_name, *, extra=None):
    args = [
        "wa_seed_campaign",
        "--tenant-label", tenant,
        "--number-label", number_label,
        "--number-phone", phone,
        "--contact", contact,
        "--campaign-name", camp_name,
        "--campaign-type", "followup",
        "--template", "Ciao {nome}",
    ]
    if extra:
        args += extra
    return args


@pytest.mark.asyncio
async def test_1_dry_run_non_scrive_nulla(db_session, monkeypatch, capsys):
    label = f"QAM3-dryrun-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(sys, "argv", _argv(
        label, "num", "+391112223330", "+391112223331", "camp",
        extra=["--dry-run"]))
    await seed.main()
    out = capsys.readouterr().out
    assert "dry-run" in out and "nessuna scrittura" in out

    from app.models.tenant import Tenant
    esiste = await db_session.scalar(select(Tenant).where(Tenant.name == label))
    assert esiste is None


@pytest.mark.asyncio
async def test_2_seed_reale_crea_le_righe_del_contratto(db_session, monkeypatch):
    from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                               WaContact, WaContactStatus, WaNumber,
                               WaNumberStatus, WaSendCondition, WaSequenceStep)
    from app.models.tenant import Tenant

    label = f"QAM3-seed-{uuid.uuid4().hex[:8]}"
    phone = f"+3911{uuid.uuid4().int % 10**8:08d}"
    contact_phone = f"+3912{uuid.uuid4().int % 10**8:08d}"
    monkeypatch.setattr(sys, "argv", _argv(
        label, "numQAM3", phone, contact_phone, f"camp-{label}",
        extra=["--force-number-active", "--start", "--daily-cap", "7"]))
    await seed.main()

    tenant = await db_session.scalar(select(Tenant).where(Tenant.name == label))
    assert tenant is not None and tenant.status == "active"

    from app.utils.phone_pseudonym import hmac_phone
    number = await db_session.scalar(
        select(WaNumber).where(WaNumber.phone_hmac == hmac_phone(phone)))
    assert number.status == WaNumberStatus.active
    assert number.warmup_day == 1
    assert number.sent_today == 0
    assert number.daily_cap == 7

    contact = await db_session.scalar(
        select(WaContact).where(WaContact.phone_hmac == hmac_phone(contact_phone)))
    assert contact.opted_out is False and contact.do_not_contact is False

    campaign = await db_session.scalar(
        select(WaCampaign).where(WaCampaign.name == f"camp-{label}"))
    assert campaign.status == WaCampaignStatus.running
    assert campaign.started_at is not None
    assert campaign.optout_enabled is False   # followup

    step = await db_session.scalar(
        select(WaSequenceStep).where(WaSequenceStep.campaign_id == campaign.id))
    assert step.step_index == 0
    assert step.send_condition == WaSendCondition.always

    cc = await db_session.scalar(
        select(WaCampaignContact).where(WaCampaignContact.campaign_id == campaign.id))
    assert cc.status == WaContactStatus.queued
    assert cc.current_step == -1
    assert cc.next_action_at is not None
    assert cc.locked_by is None and cc.locked_at is None
    assert cc.failure_count == 0


@pytest.mark.asyncio
async def test_3_seed_e_idempotente(db_session, monkeypatch):
    from app.models.wa import WaCampaign, WaCampaignContact, WaContact, WaNumber
    from app.models.tenant import Tenant

    label = f"QAM3-idem-{uuid.uuid4().hex[:8]}"
    phone = f"+3913{uuid.uuid4().int % 10**8:08d}"
    contact_phone = f"+3914{uuid.uuid4().int % 10**8:08d}"
    argv = _argv(label, "numQAM3", phone, contact_phone, f"camp-{label}",
                extra=["--force-number-active", "--start"])

    monkeypatch.setattr(sys, "argv", argv)
    await seed.main()
    monkeypatch.setattr(sys, "argv", argv)
    await seed.main()   # stesso comando, ripetuto

    async def _count(model, **where):
        from sqlalchemy import func
        q = select(func.count(model.id))
        for k, v in where.items():
            q = q.where(getattr(model, k) == v)
        return await db_session.scalar(q)

    assert await _count(Tenant, name=label) == 1
    assert await _count(WaNumber, tenant_id=(
        await db_session.scalar(select(Tenant).where(Tenant.name == label))).id) == 1
    assert await _count(WaContact) >= 1
    campaign = await db_session.scalar(
        select(WaCampaign).where(WaCampaign.name == f"camp-{label}"))
    assert await _count(WaCampaignContact, campaign_id=campaign.id) == 1


def test_4_seed_rifiuta_db_non_test():
    with pytest.raises(SystemExit) as exc:
        seed._assert_db_di_test("postgresql://x/prod_fake", forzato=False)
    assert "prod_fake" not in str(exc.value) or "postgresql" in str(exc.value)
    # non solleva se forzato
    seed._assert_db_di_test("postgresql://x/prod_fake", forzato=True)


def test_5_seed_rifiuta_profilo_poc1():
    with pytest.raises(SystemExit) as exc:
        seed._assert_profilo_non_poc1(r"D:\dev\wa-poc\profile")
    assert "PoC-1" in str(exc.value)
    # path diverso non solleva
    seed._assert_profilo_non_poc1(r"data/browser_profiles/altro")
    seed._assert_profilo_non_poc1(None)
