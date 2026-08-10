# backend/tests/test_scrape_batch_errore_inatteso.py
"""C2 Parte 2 (difesa in profondita'): un'eccezione IMPREVISTA dentro
fetch_and_store_bio_browser (non solo l'IntegrityError che la Parte 1
previene) deve marcare il follower invece di lasciarlo pending. Senza questo,
il prossimo giro del batch ripesca la STESSA riga (limit(1) senza ORDER BY,
vedi commento nel loop) -> stesso errore -> retry infinito, batch dopo batch.
"""
from datetime import datetime
import uuid
import pytest

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.services import browser_bio


@pytest.mark.asyncio
async def test_eccezione_imprevista_marca_skipped_non_lascia_pending(monkeypatch):
    base = 974000000000 + int(datetime.utcnow().timestamp()) % 100000
    async with AsyncSessionLocal() as db:
        camp = Campaign(name="t-errore-inatteso", status=CampaignStatus.scraping, source_type="scrape")
        db.add(camp)
        await db.flush()
        db.add(Follower(campaign_id=camp.id, ig_user_id=base,
                        username=f"u{base}", status=FollowerStatus.pending))
        await db.commit()
        cid = camp.id

    async def fake_fetch_esplode(follower, campaign, db, session):
        raise RuntimeError("errore inatteso simulato (es. deadlock/timeout Postgres)")
    monkeypatch.setattr(browser_bio, "fetch_and_store_bio_browser", fake_fetch_esplode)

    async with AsyncSessionLocal() as db:
        camp = await db.get(Campaign, cid)
        done = await browser_bio._scrape_batch(
            camp, db, browser_session=None, count=5, account_id=str(uuid.uuid4()),
        )

    assert done == 0

    async with AsyncSessionLocal() as db:
        from sqlalchemy import select
        f = (await db.execute(
            select(Follower).where(Follower.campaign_id == cid)
        )).scalar_one()
        assert f.status == FollowerStatus.skipped, (
            "senza la marcatura il prossimo giro ripesca la STESSA riga pending -> loop infinito"
        )
        assert f.skip_reason == "browser_errore_inatteso"
