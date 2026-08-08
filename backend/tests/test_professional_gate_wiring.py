"""Passo 4 / A.3 — il gate professional e' davvero ATTACCATO ai percorsi reali.

Perche' questo file esiste separato da test_professional_gate.py: quello prova che
la REGOLA e' giusta, e passerebbe anche se il punto di chiamata passasse l'oggetto
sbagliato. `contatti_richiesti` riceve il payload grezzo (un dict); se un chiamante
gli passasse lo shim (un SimpleNamespace) il gate leggerebbe "nessun segnale", che
per progetto significa "procedi", e sarebbe SEMPRE APERTO -- silenziosamente
inutile, con tutti i test unitari verdi. E' la stessa fail-mode del fake che
consegnava la risposta dentro il goto: verde e senza valore.

Qui si esercitano le due funzioni vere (Fase Bio e risoluzione import) contando se
`/info/` -- l'unica richiesta che nessuna pagina web produce mai -- e' partita.
"""
import pytest

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus, ENRICHMENT_CONTACTS
from app.models.follower import Follower, FollowerStatus
from app.models.imported_profile import ImportedProfile
from app.services import browser_bio, browser_import as bi


class _FakeSession:
    def __init__(self, *a, **k):
        pass

    async def open(self):
        return None

    async def close(self):
        return None

    class _P:
        async def ensure_logged_in(self, account_id, allow_login=True):
            return None

        async def _get_page(self):
            return object()

        async def browse_reels(self, *a, **k):
            return None

    page = _P()


def _payload(pk, username, **extra):
    """Payload nella forma di web_profile_info. `extra` inietta i campi del gate."""
    base = {
        "id": str(pk), "username": username, "full_name": "Full Name",
        "biography": "ciao bio", "is_verified": False, "is_private": False,
        "edge_followed_by": {"count": 1200}, "edge_follow": {"count": 300},
        "external_url": "", "business_email": None, "business_phone_number": None,
        "bio_links": [],
    }
    base.update(extra)
    return base


class _SpiaInfo:
    """Registra ogni chiamata a /info/. Sostituisce _fetch_public_contact_inpage."""

    def __init__(self):
        self.chiamate = []

    async def __call__(self, raw_page, pk):
        self.chiamate.append(pk)
        return {"public_email": "vero@shop.it"}


# --------------------------------------------------------------------------- #
# Percorso Fase Bio (source_type='scrape')
# --------------------------------------------------------------------------- #
async def _campagna_con_follower(pk, username):
    async with AsyncSessionLocal() as db:
        camp = Campaign(
            name=f"gate-{username}", status=CampaignStatus.scraping,
            source_type="scrape", bio_engine="browser",
            enrichment_level=ENRICHMENT_CONTACTS,
        )
        db.add(camp)
        await db.flush()
        f = Follower(campaign_id=camp.id, ig_user_id=pk, username=username,
                     status=FollowerStatus.pending)
        db.add(f)
        await db.commit()
        return camp.id, f.id


@pytest.mark.asyncio
@pytest.mark.parametrize("extra, info_attesa", [
    ({"is_professional_account": True}, True),
    ({"account_type": 2}, True),
    ({"is_professional_account": None, "account_type": 1}, False),
    ({"is_business": False}, False),
])
async def test_fase_bio_chiama_info_solo_sui_professional(monkeypatch, extra, info_attesa):
    pk = 981200000001 + abs(hash(str(extra))) % 900000
    cid, fid = await _campagna_con_follower(pk, f"u{pk}")

    async def fake_capture(raw_page, username, timeout_s=None):
        return _payload(pk, username, **extra)

    spia = _SpiaInfo()
    monkeypatch.setattr(browser_bio, "_capture_web_profile_info", fake_capture)
    monkeypatch.setattr(browser_bio, "_fetch_public_contact_inpage", spia)
    monkeypatch.setattr(browser_bio.settings, "bio_browser_contact_info_enabled", True)
    monkeypatch.setattr(browser_bio.settings, "bio_browser_professional_gate_enabled", True)

    async with AsyncSessionLocal() as db:
        camp = await db.get(Campaign, cid)
        f = await db.get(Follower, fid)
        await browser_bio.fetch_and_store_bio_browser(f, camp, db, _FakeSession())

    assert bool(spia.chiamate) is info_attesa, (
        f"payload {extra}: /info/ chiamata={bool(spia.chiamate)}, attesa={info_attesa}. "
        "Se e' sempre True il gate non e' collegato (payload sbagliato al chiamante)."
    )


# --------------------------------------------------------------------------- #
# Percorso risoluzione import (source_type='import')
# --------------------------------------------------------------------------- #
async def _campagna_import_con_riga(username):
    async with AsyncSessionLocal() as db:
        camp = Campaign(
            name=f"gate-imp-{username}", status=CampaignStatus.scraping,
            source_type="import", bio_engine="browser", messaging_enabled=True,
            enrichment_level=ENRICHMENT_CONTACTS,
        )
        db.add(camp)
        await db.flush()
        row = ImportedProfile(campaign_id=camp.id, raw_input=username,
                              username=username, status="resolving")
        db.add(row)
        await db.commit()
        return camp.id, row.id


@pytest.mark.asyncio
@pytest.mark.parametrize("extra, info_attesa", [
    ({"is_professional_account": True}, True),
    ({"account_type": 3}, True),
    ({"account_type": 1}, False),
    ({"is_professional_account": False}, False),
])
async def test_import_chiama_info_solo_sui_professional(monkeypatch, extra, info_attesa):
    pk = 982300000001 + abs(hash(str(extra))) % 900000
    username = f"imp{pk}"
    cid, rid = await _campagna_import_con_riga(username)

    async def fake_capture(raw_page, uname, timeout_s=None):
        return _payload(pk, uname, **extra)

    spia = _SpiaInfo()
    monkeypatch.setattr(bi, "_capture_web_profile_info", fake_capture)
    monkeypatch.setattr(bi, "_fetch_public_contact_inpage", spia)
    monkeypatch.setattr(bi.settings, "bio_browser_contact_info_enabled", True)
    monkeypatch.setattr(bi.settings, "bio_browser_professional_gate_enabled", True)

    async with AsyncSessionLocal() as db:
        camp = await db.get(Campaign, cid)
        row = await db.get(ImportedProfile, rid)
        outcome, err = await bi.resolve_and_store_bio_browser(row, camp, db, _FakeSession())

    assert outcome == "done", f"risoluzione non riuscita: {outcome} {err!r}"
    assert bool(spia.chiamate) is info_attesa, (
        f"payload {extra}: /info/ chiamata={bool(spia.chiamate)}, attesa={info_attesa}"
    )


@pytest.mark.asyncio
async def test_il_gate_riceve_un_dict_non_lo_shim(monkeypatch):
    """La fail-mode specifica, catturata direttamente: se il chiamante passasse lo
    shim invece del payload, il gate non troverebbe nessun segnale e resterebbe
    aperto per sempre. Qui si registra COSA riceve."""
    ricevuti = []
    vero = browser_bio.contatti_richiesti

    def spia_contatti(campaign, profilo=None):
        ricevuti.append(profilo)
        return vero(campaign, profilo)

    pk = 983400000001
    cid, fid = await _campagna_con_follower(pk, f"u{pk}")

    async def fake_capture(raw_page, username, timeout_s=None):
        return _payload(pk, username, account_type=1)

    monkeypatch.setattr(browser_bio, "_capture_web_profile_info", fake_capture)
    monkeypatch.setattr(browser_bio, "_fetch_public_contact_inpage", _SpiaInfo())
    monkeypatch.setattr(browser_bio, "contatti_richiesti", spia_contatti)

    async with AsyncSessionLocal() as db:
        camp = await db.get(Campaign, cid)
        f = await db.get(Follower, fid)
        await browser_bio.fetch_and_store_bio_browser(f, camp, db, _FakeSession())

    assert ricevuti, "contatti_richiesti non e' stata chiamata affatto"
    profilo = ricevuti[0]
    assert isinstance(profilo, dict), f"il gate ha ricevuto {type(profilo).__name__}, non un dict"
    assert profilo.get("account_type") == 1, "il segnale del gate non e' arrivato intatto"
