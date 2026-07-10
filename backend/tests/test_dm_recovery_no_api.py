"""Recovery checker SENZA lettura API.

Un Message fermo in 'sending' (Invio premuto, commit 'sent' perso) viene marcato
`failed` — terminale, nessun reinvio automatico, NESSUNA chiamata API Instagram
(la vecchia verifica `direct_threads` era il pattern-API-nudo da checkpoint).
"""
import asyncio
import uuid
from datetime import datetime, timedelta

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.models.message import Message, MessageStatus
from app.services.recovery_checker import recover_sending_messages


def _seed_stale_sending() -> tuple[str, str]:
    cid = str(uuid.uuid4())
    fid = str(uuid.uuid4())
    mid = str(uuid.uuid4())
    old = datetime.utcnow() - timedelta(minutes=20)  # oltre il cutoff di 10 min

    async def _seed():
        async with AsyncSessionLocal() as db:
            db.add(Campaign(
                id=cid, name=f"rec-{cid[:6]}", source_type="scrape",
                target_username="t", scrape_mode="followers",
                status=CampaignStatus.running,
            ))
            db.add(Follower(
                id=fid, campaign_id=cid, ig_user_id=uuid.uuid4().int % 10_000_000,
                username="target_rec", status=FollowerStatus.message_generated,
                locked_by_account_id="acc-x", locked_at=old,
            ))
            db.add(Message(
                id=mid, campaign_id=cid, follower_id=fid, account_id=None,
                generated_text="Ciao, messaggio in volo.",
                status=MessageStatus.sending, updated_at=old,
            ))
            await db.commit()

    asyncio.run(_seed())
    return mid, fid


def _fetch(mid: str, fid: str):
    async def _get():
        async with AsyncSessionLocal() as db:
            m = await db.get(Message, mid)
            f = await db.get(Follower, fid)
            return m.status, m.error_message, f.status, f.locked_by_account_id

    return asyncio.run(_get())


def test_stale_sending_marcato_failed_senza_api():
    mid, fid = _seed_stale_sending()

    counts = asyncio.run(recover_sending_messages())

    m_status, m_err, f_status, f_lock = _fetch(mid, fid)
    assert m_status == MessageStatus.failed          # terminale, no resend
    assert "verifica API disattivata" in (m_err or "")
    assert f_status == FollowerStatus.failed
    assert f_lock is None                             # lock rilasciato
    assert counts["skipped"] >= 1                     # nessun recovered via API


def test_sending_recente_non_toccato():
    # updated_at dentro il cutoff (2 min fa) -> il recovery NON lo processa
    cid = str(uuid.uuid4())
    fid = str(uuid.uuid4())
    mid = str(uuid.uuid4())
    recent = datetime.utcnow() - timedelta(minutes=2)

    async def _seed():
        async with AsyncSessionLocal() as db:
            db.add(Campaign(id=cid, name=f"rec2-{cid[:6]}", source_type="scrape",
                            target_username="t", scrape_mode="followers",
                            status=CampaignStatus.running))
            db.add(Follower(id=fid, campaign_id=cid, ig_user_id=uuid.uuid4().int % 10_000_000,
                            username="target_recent", status=FollowerStatus.message_generated))
            db.add(Message(id=mid, campaign_id=cid, follower_id=fid, account_id=None,
                           generated_text="msg recente", status=MessageStatus.sending,
                           updated_at=recent))
            await db.commit()

    asyncio.run(_seed())
    asyncio.run(recover_sending_messages())

    async def _get():
        async with AsyncSessionLocal() as db:
            return (await db.get(Message, mid)).status

    assert asyncio.run(_get()) == MessageStatus.sending  # ancora sending, non toccato
