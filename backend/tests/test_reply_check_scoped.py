"""Reply-check ristretto: solo campagne ATTIVE + invii RECENTI.

Riduce il footprint API (la lettura inbox e' tracciabile come bot -> rischio
checkpoint). Prima leggeva l'inbox ogni 30 min anche per campagne paused/completed,
per sempre. Ora: solo running/scraping_and_running + follower contattati negli
ultimi reply_check_max_age_days.
"""
import asyncio
import uuid
from datetime import datetime, timedelta

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount, AccountStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.campaign_account import CampaignAccount
from app.models.follower import Follower, FollowerStatus
from app.models.message import Message, MessageStatus
from app.services import reply_checker


def _mk_campaign(db, status, completed_at=None):
    cid = str(uuid.uuid4())
    db.add(Campaign(id=cid, name=f"rc-{cid[:6]}", source_type="scrape",
                    target_username="t", scrape_mode="followers", status=status,
                    completed_at=completed_at))
    return cid


# ── filtro STATO: attive SEMPRE; completed solo entro la finestra di grazia ──

def test_solo_campagne_attive_scansionate(monkeypatch):
    ids = {}

    async def _seed():
        async with AsyncSessionLocal() as db:
            ids["running"] = _mk_campaign(db, CampaignStatus.running)
            ids["parr"] = _mk_campaign(db, CampaignStatus.scraping_and_running)
            ids["paused"] = _mk_campaign(db, CampaignStatus.paused)
            # completed SENZA completed_at (vecchie) resta esclusa
            ids["completed"] = _mk_campaign(db, CampaignStatus.completed)
            await db.commit()
    asyncio.run(_seed())

    scanned = []

    async def _fake_check_campaign(campaign_id, db):
        scanned.append(campaign_id)
        return 0
    monkeypatch.setattr(reply_checker, "_check_campaign", _fake_check_campaign)

    asyncio.run(reply_checker.check_all_replies())

    assert ids["running"] in scanned
    assert ids["parr"] in scanned
    assert ids["paused"] in scanned          # paused: le risposte arrivano lo stesso
    assert ids["completed"] not in scanned   # completed senza completed_at: esclusa


def test_paused_controllata_ma_senza_costo_api_se_invii_vecchi(monkeypatch):
    """Le paused vanno controllate (Tommaso mette in pausa di continuo: se le
    saltiamo le risposte non si vedono mai). Il freno non e' lo stato ma il
    cutoff per-follower: senza invii recenti _check_campaign esce PRIMA di
    qualsiasi login/chiamata API -> zero footprint per le campagne morte."""
    cid = str(uuid.uuid4())
    aid = str(uuid.uuid4())
    old_pk = uuid.uuid4().int % 10_000_000

    async def _seed():
        async with AsyncSessionLocal() as db:
            db.add(Campaign(id=cid, name=f"pz-{cid[:6]}", source_type="scrape",
                            target_username="t", scrape_mode="followers",
                            status=CampaignStatus.paused))
            db.add(InstagramAccount(id=aid, username=f"acc_{aid[:6]}",
                                    encrypted_password="x", status=AccountStatus.active))
            db.add(CampaignAccount(campaign_id=cid, account_id=aid, role="both", is_active=True))
            fid = str(uuid.uuid4())
            db.add(Follower(id=fid, campaign_id=cid, ig_user_id=old_pk,
                            username=f"old_{fid[:4]}", status=FollowerStatus.sent))
            db.add(Message(campaign_id=cid, follower_id=fid, account_id=aid,
                           generated_text="ciao", status=MessageStatus.sent,
                           sent_at=datetime.utcnow() - timedelta(days=settings.reply_check_max_age_days + 5)))
            await db.commit()
    asyncio.run(_seed())

    logins = []

    async def _fake_login(account, db, skip_gql_verify=False):
        logins.append(account.username)
        raise AssertionError("non deve loggarsi: nessun invio recente")
    monkeypatch.setattr(reply_checker, "_login", _fake_login)

    asyncio.run(reply_checker.check_all_replies())

    assert logins == []   # invii vecchi -> nessuna chiamata API nonostante paused


def test_completed_entro_finestra_grazia_ancora_scansionata(monkeypatch):
    """Una campagna completata da poco deve ancora essere controllata per le
    risposte tardive (caso reale: PODCAST 1 BORDERLINE completed, risposte a 24h
    non tracciate). Dopo la finestra di grazia (default 3gg) si smette."""
    ids = {}
    grace = settings.reply_check_completed_grace_days

    async def _seed():
        async with AsyncSessionLocal() as db:
            now = datetime.utcnow()
            ids["recent"] = _mk_campaign(db, CampaignStatus.completed,
                                         completed_at=now - timedelta(days=grace - 1))
            ids["old"] = _mk_campaign(db, CampaignStatus.completed,
                                      completed_at=now - timedelta(days=grace + 2))
            await db.commit()
    asyncio.run(_seed())

    scanned = []

    async def _fake_check_campaign(campaign_id, db):
        scanned.append(campaign_id)
        return 0
    monkeypatch.setattr(reply_checker, "_check_campaign", _fake_check_campaign)

    asyncio.run(reply_checker.check_all_replies())

    assert ids["recent"] in scanned      # completata da <3gg: ancora controllata
    assert ids["old"] not in scanned     # completata da >3gg: basta


# ── filtro ETA': solo invii recenti diventano candidati ────────────────────

def test_solo_invii_recenti_candidati(monkeypatch):
    cid = str(uuid.uuid4())
    aid = str(uuid.uuid4())
    recent_pk = uuid.uuid4().int % 10_000_000
    old_pk = recent_pk + 1

    async def _seed():
        async with AsyncSessionLocal() as db:
            db.add(Campaign(id=cid, name=f"age-{cid[:6]}", source_type="scrape",
                            target_username="t", scrape_mode="followers",
                            status=CampaignStatus.running))
            db.add(InstagramAccount(id=aid, username=f"acc_{aid[:6]}",
                                    encrypted_password="x", status=AccountStatus.active))
            db.add(CampaignAccount(campaign_id=cid, account_id=aid, role="both", is_active=True))
            now = datetime.utcnow()
            for pk, sent_at, label in [
                (recent_pk, now - timedelta(days=2), "recent"),
                (old_pk, now - timedelta(days=settings.reply_check_max_age_days + 5), "old"),
            ]:
                fid = str(uuid.uuid4())
                db.add(Follower(id=fid, campaign_id=cid, ig_user_id=pk,
                                username=f"{label}_{fid[:4]}", status=FollowerStatus.sent))
                db.add(Message(campaign_id=cid, follower_id=fid, account_id=aid,
                               generated_text="ciao", status=MessageStatus.sent, sent_at=sent_at))
            await db.commit()
    asyncio.run(_seed())

    captured = {}

    async def _fake_scan(account, sent_followers, db):
        captured.update(sent_followers)
        return 0
    monkeypatch.setattr(reply_checker, "_scan_inbox", _fake_scan)

    asyncio.run(reply_checker.check_all_replies())

    assert recent_pk in captured        # invio 2 giorni fa -> candidato
    assert old_pk not in captured       # invio oltre la finestra -> escluso
