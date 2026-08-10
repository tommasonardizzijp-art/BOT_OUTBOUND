# backend/tests/test_browser_bio_targa_collisione.py
"""C2 CRITICAL: decidi_sostituzione_targa == 'sostituisci' non controllava se
un'ALTRA riga della stessa campagna avesse gia' quel pk (username con la
chiocciola gia' in DB, vedi targa.py, o rename riassegnato). Il commit finale
sollevava IntegrityError su UniqueConstraint(campaign_id, ig_user_id); il loop
batch (limit(1) senza ORDER BY) ripescava la STESSA riga pending ad ogni giro
-> la Fase Bio si bloccava per sempre su quella campagna.
"""
from datetime import datetime
import pytest

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.services import browser_bio


class _FakePage:
    def __init__(self, user):
        self._user = user

    async def _get_page(self):
        return self  # non usato: patchiamo _capture_web_profile_info


class _FakeSession:
    def __init__(self, user):
        self.page = _FakePage(user)


@pytest.mark.asyncio
async def test_targa_gia_presente_su_altra_riga_skip_senza_integrity_error(monkeypatch):
    """Due Follower della stessa campagna: uno con targa provvisoria (quello che
    stiamo arricchendo), un altro che ha GIA' come ig_user_id il pk vero che
    l'arricchimento del primo sta per assegnare. Deve uscire 'skipped', senza
    IntegrityError, e senza toccare i dati del secondo."""
    pk_vero = 991000000000 + int(datetime.utcnow().timestamp()) % 100000
    async with AsyncSessionLocal() as db:
        camp = Campaign(name="t-collisione-targa", status=CampaignStatus.scraping, source_type="scrape")
        db.add(camp)
        await db.flush()

        bersaglio = Follower(
            campaign_id=camp.id, ig_user_id=pk_vero, username="bersaglio_esistente",
            full_name="Bersaglio Esistente", biography="bio originale intatta",
            status=FollowerStatus.bio_scraped,
        )
        provvisorio = Follower(
            campaign_id=camp.id, ig_user_id=-123456789, username="in_arricchimento",
            status=FollowerStatus.pending,
            locked_by_account_id="acc-1", locked_at=datetime.utcnow(),
        )
        db.add(bersaglio)
        db.add(provvisorio)
        await db.commit()
        await db.refresh(bersaglio)
        await db.refresh(provvisorio)

        async def fake_capture(raw_page, username, timeout_s=8.0):
            return {"id": str(pk_vero), "username": username, "full_name": "In Arricchimento",
                    "biography": "bio nuova", "edge_followed_by": {"count": 1},
                    "edge_follow": {"count": 1}}
        monkeypatch.setattr(browser_bio, "_capture_web_profile_info", fake_capture)

        outcome, err = await browser_bio.fetch_and_store_bio_browser(
            provvisorio, camp, db, _FakeSession({}),
        )

        assert outcome == "skipped"
        assert err is None

        await db.refresh(provvisorio)
        assert provvisorio.status == FollowerStatus.skipped
        assert provvisorio.skip_reason == "targa_gia_presente_su_altra_riga"
        assert provvisorio.ig_user_id == -123456789, "la targa provvisoria NON va sostituita in caso di collisione"
        assert provvisorio.locked_by_account_id is None
        assert provvisorio.locked_at is None

        await db.refresh(bersaglio)
        assert bersaglio.biography == "bio originale intatta", "nessun merge automatico fra le due righe"
        assert bersaglio.full_name == "Bersaglio Esistente"
        assert bersaglio.status == FollowerStatus.bio_scraped
