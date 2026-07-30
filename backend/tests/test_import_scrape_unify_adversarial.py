"""Adversarial pass di chiusura modulo import/scrape unify.

Criterio INVERTITO (standard sviluppo-modulo): un test passa se il sistema SI DIFENDE
(eccezione chiara 400/404, nessuna scrittura sporca) — non se "sembra funzionare".
Ogni caso e' testato con un assert CONCRETO sullo stato DB dopo l'operazione.

Pattern preso da test_campaigns_import_retry_failed.py / test_campaigns_import_reset.py:
chiamata diretta alle funzioni async via AsyncSessionLocal (niente fixture
`campaign_factory` ne' client HTTP condiviso nella suite).
"""
import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.account import AccountStatus, InstagramAccount
from app.models.campaign import Campaign, CampaignStatus
from app.models.campaign_account import CampaignAccount
from app.models.follower import Follower, FollowerStatus
from app.models.imported_profile import ImportedProfile


async def _campaign(db, source_type="import", status=CampaignStatus.ready, **kw) -> Campaign:
    campaign = Campaign(
        id=str(uuid.uuid4()),
        name="adversarial-test",
        source_type=source_type,
        target_username="target_user",
        scrape_mode="followers",
        status=status,
        messaging_enabled=kw.pop("messaging_enabled", False),
        **kw,
    )
    db.add(campaign)
    await db.flush()
    return campaign


async def _account(db, role: str, is_active: bool = True) -> InstagramAccount:
    acc = InstagramAccount(
        username=f"acc_{role}_{uuid.uuid4().hex[:8]}",
        status=AccountStatus.active,
        session_data="{}",
        encrypted_password="x",
    )
    db.add(acc)
    await db.flush()
    return acc


# ---------------------------------------------------------------------------
# 1. Concorrenza vera su import_retry_failed (asyncio.gather, non sequenziale)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_concurrent_retry_failed_does_not_duplicate_rows():
    from app.api import campaigns as capi

    async with AsyncSessionLocal() as db:
        campaign = await _campaign(db)
        db.add(ImportedProfile(
            campaign_id=campaign.id, raw_input="bad_a", username="bad_a", status="not_found",
        ))
        db.add(ImportedProfile(
            campaign_id=campaign.id, raw_input="bad_b", username="bad_b", status="error", error="timeout",
        ))
        await db.commit()
        campaign_id = campaign.id

    async def _call():
        async with AsyncSessionLocal() as db2:
            try:
                return await capi.import_retry_failed(campaign_id, db2)
            except HTTPException as exc:
                return exc

    results = await asyncio.gather(_call(), _call(), return_exceptions=True)

    # nessuna eccezione NON gestita (HTTPException e' gestita esplicitamente sopra)
    for r in results:
        assert not isinstance(r, Exception) or isinstance(r, HTTPException), (
            f"eccezione non gestita durante concorrenza: {r!r}"
        )

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(ImportedProfile.username, ImportedProfile.status)
            .where(ImportedProfile.campaign_id == campaign_id)
        )).all()

    # nessuna riga duplicata: ancora esattamente 2 righe, entrambe pending
    assert len(rows) == 2
    by_username = {u: s for u, s in rows}
    assert by_username["bad_a"] == "pending"
    assert by_username["bad_b"] == "pending"


# ---------------------------------------------------------------------------
# 2. Doppio reset consecutivo su campagna import con righe pending
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_double_reset_is_idempotent():
    from app.api import campaigns as capi

    async with AsyncSessionLocal() as db:
        campaign = await _campaign(db, status=CampaignStatus.paused)
        db.add(ImportedProfile(
            campaign_id=campaign.id, raw_input="still_pending", username="still_pending", status="pending",
        ))
        await db.commit()
        campaign_id = campaign.id

    async with AsyncSessionLocal() as db:
        result1 = await capi.reset_campaign(campaign_id, db)
    assert result1.status == "error"  # righe pending residue -> error (sintomo A)

    # secondo reset consecutivo: status=error e' nella whitelist resettabile
    async with AsyncSessionLocal() as db:
        result2 = await capi.reset_campaign(campaign_id, db)
    assert result2.status == "error"  # idempotente: stesso esito, nessun crash

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(ImportedProfile.status)
            .where(ImportedProfile.campaign_id == campaign_id)
        )).scalars().all()
    assert rows == ["pending"]  # stato coerente, nessuna riga persa/duplicata


# ---------------------------------------------------------------------------
# 3. Requeue su campagna senza ImportedProfile rows -> 400 pulito
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_retry_failed_on_empty_campaign_returns_400_not_crash():
    from app.api import campaigns as capi

    async with AsyncSessionLocal() as db:
        campaign = await _campaign(db)
        await db.commit()

        with pytest.raises(HTTPException) as exc_info:
            await capi.import_retry_failed(campaign.id, db)

    assert exc_info.value.status_code == 400
    assert "Nessun profilo fallito" in exc_info.value.detail


# ---------------------------------------------------------------------------
# 4. start-dm-auto con account scraping-only disattivato a runtime -> 400
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_start_dm_auto_rejects_when_scrape_account_deactivated_after_assignment(monkeypatch):
    from app.api import campaigns as capi

    async def _redis_ok():
        return True

    monkeypatch.setattr(capi, "_check_redis_reachable", _redis_ok)

    async with AsyncSessionLocal() as db:
        campaign = await _campaign(
            db, status=CampaignStatus.scraping, auto_generate=True,
            messaging_enabled=True, base_message_template="ciao " * 5,
        )
        acc_scrape = await _account(db, "scraping")
        acc_dm = await _account(db, "dm")
        ca_scrape = CampaignAccount(campaign_id=campaign.id, account_id=acc_scrape.id, role="scraping", is_active=True)
        db.add(ca_scrape)
        db.add(CampaignAccount(campaign_id=campaign.id, account_id=acc_dm.id, role="dm", is_active=True))
        await db.commit()

        # disattivazione a runtime, DOPO l'assegnazione
        ca_scrape.is_active = False
        await db.commit()

        with pytest.raises(HTTPException) as exc_info:
            await capi.start_dm_auto(campaign.id, db)

    assert exc_info.value.status_code == 400

    async with AsyncSessionLocal() as db:
        refreshed = await db.scalar(select(Campaign).where(Campaign.id == campaign.id))
    assert refreshed.status == CampaignStatus.scraping  # stato macchina invariato


# ---------------------------------------------------------------------------
# 5. reset_campaign su campagna source_type="scrape" -> sempre draft (regressione)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_reset_scrape_campaign_always_goes_to_draft():
    from app.api import campaigns as capi

    async with AsyncSessionLocal() as db:
        campaign = await _campaign(db, source_type="scrape", status=CampaignStatus.error)
        db.add(Follower(
            campaign_id=campaign.id, ig_user_id=1, username="resolved_one",
            status=FollowerStatus.bio_scraped,
        ))
        await db.commit()

        result = await capi.reset_campaign(campaign.id, db)

    assert result.status == "draft"  # il ramo import a 3 vie non deve toccare scrape


# ---------------------------------------------------------------------------
# 6. import_retry_failed su campagna con SOLO righe 'private' -> 400
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_retry_failed_with_only_private_rows_returns_400():
    from app.api import campaigns as capi

    async with AsyncSessionLocal() as db:
        campaign = await _campaign(db)
        db.add(ImportedProfile(
            campaign_id=campaign.id, raw_input="priv_a", username="priv_a", status="private",
        ))
        db.add(ImportedProfile(
            campaign_id=campaign.id, raw_input="priv_b", username="priv_b", status="private",
        ))
        await db.commit()

        with pytest.raises(HTTPException) as exc_info:
            await capi.import_retry_failed(campaign.id, db)

    assert exc_info.value.status_code == 400

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(ImportedProfile.status)
            .where(ImportedProfile.campaign_id == campaign.id)
        )).scalars().all()
    assert set(rows) == {"private"}  # non toccate


# ---------------------------------------------------------------------------
# 7. Reset con campagna import completamente vuota -> draft, no crash
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_reset_completely_empty_import_campaign_goes_to_draft():
    from app.api import campaigns as capi

    async with AsyncSessionLocal() as db:
        campaign = await _campaign(db, status=CampaignStatus.error)
        await db.commit()

        result = await capi.reset_campaign(campaign.id, db)

    assert result.status == "draft"


# ---------------------------------------------------------------------------
# 8. Numeri ostili: campaign_id inesistente -> 404 pulito (via _get_or_404)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_retry_failed_nonexistent_campaign_returns_404():
    from app.api import campaigns as capi

    fake_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        with pytest.raises(HTTPException) as exc_info:
            await capi.import_retry_failed(fake_id, db)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_reset_nonexistent_campaign_returns_404():
    from app.api import campaigns as capi

    fake_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        with pytest.raises(HTTPException) as exc_info:
            await capi.reset_campaign(fake_id, db)

    assert exc_info.value.status_code == 404
