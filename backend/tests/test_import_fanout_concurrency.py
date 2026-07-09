"""FAN-OUT concurrency proof per l'import via BROWSER (bio_engine='browser').

Attacca gli INVARIANTI del fan-out per-account (mirror della Fase Bio browser):

  (1) NON-DOPPIA-RISOLUZIONE — due account concorrenti non prendono MAI lo stesso
      ImportedProfile: nessun Follower duplicato, nessun doppio browser footprint.
      Il claim atomico status-flip (pending -> resolving, con recupero stale) e' l'unica
      primitiva di concorrenza (ImportedProfile non ha colonne di lock come Follower).
  (2) NESSUNA-RIGA-PERSA — ogni riga pending raggiunge uno stato terminale.
  (3) COMPLETAMENTO-ESATTAMENTE-UNA-VOLTA — la campagna completa solo quando pending+resolving
      == 0, e la transizione avviene una sola volta (UPDATE atomico condizionato su status).
  (4) STALE-RECOVERY — una 'resolving' orfana (updated_at oltre LOCK_TIMEOUT) viene recuperata
      e risolta, NON persa; una 'resolving' FRESCA (sessione viva lenta) NON viene rubata.
  (5) NO-PREMATURE-COMPLETE — finche' una riga e' 'resolving', _complete_import_browser NON
      completa e la campagna resta 'scraping'.

SICUREZZA: nessun IG/Redis/browser reale. conftest forza sqlite di test.
Stile: gemello di tests/test_browser_import.py.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.models.imported_profile import ImportedProfile
from app.services import browser_import as bi


# --------------------------------------------------------------------------- #
# Fakes / helpers (allineati a tests/test_browser_import.py)
# --------------------------------------------------------------------------- #
class _FakeSession:
    def __init__(self, *a, **k):
        self.opened = False
        self.closed = False

    async def open(self):
        self.opened = True

    async def close(self):
        self.closed = True

    class _P:
        async def ensure_logged_in(self, account_id, allow_login=True):
            return None

        async def _get_page(self):
            return object()

        async def browse_reels(self, *a, **k):
            return None

    page = _P()


async def _anoop(*a, **k):
    return None


async def _anoop_false(*a, **k):
    return False


async def _mk_import_campaign(engine="browser", n_pending=0, messaging=True):
    async with AsyncSessionLocal() as db:
        camp = Campaign(
            name="fanout", status=CampaignStatus.scraping,
            source_type="import", bio_engine=engine, messaging_enabled=messaging,
        )
        db.add(camp)
        await db.flush()
        for i in range(n_pending):
            db.add(ImportedProfile(
                campaign_id=camp.id, raw_input=f"u{i}", username=f"u{i}", status="pending",
            ))
        await db.commit()
        return camp.id


def _use_fake_browser(monkeypatch, cap=5):
    """Silenzia browser/pacing/soft-block/emit: nessun IG, Redis o browser reale."""
    monkeypatch.setattr(bi, "BrowserSession", _FakeSession)
    monkeypatch.setattr(bi, "pick_session_cap", lambda *a, **k: cap)
    monkeypatch.setattr(bi, "human_profile_pause", _anoop)
    monkeypatch.setattr(bi, "maybe_micro_scroll", _anoop_false)
    monkeypatch.setattr(bi, "_soft_block_reset", _anoop)      # tocca Redis -> mock
    monkeypatch.setattr(bi, "_soft_block_incr", _anoop)       # tocca Redis -> mock


def _capture_events(monkeypatch):
    """Intercetta emit_event (altrimenti tenta Redis) e ritorna la lista raccolta."""
    events = []

    def fake_emit(campaign_id, action, detail="", level="info"):
        events.append((campaign_id, action, level))
    monkeypatch.setattr(bi, "emit_event", fake_emit)
    return events


def _faithful_resolve_factory(offset=700900000):
    """Fabbrica un fake resolve_and_store_bio_browser FEDELE: marca la riga claimata
    terminale ('resolved'), assegna ig_user_id derivato dallo username (distinto per riga)
    e CREA il Follower — esattamente cio' che fa il codice reale sul ramo 'done'."""
    async def fake_resolve(row, campaign, db, session):
        pk = offset + int(row.username[1:])  # uN -> pk distinto e deterministico
        row.status = "resolved"
        row.ig_user_id = pk
        row.error = None
        row.updated_at = datetime.utcnow()
        db.add(Follower(
            campaign_id=campaign.id, ig_user_id=pk,
            username=row.username, status=FollowerStatus.bio_scraped,
        ))
        await db.commit()
        return "done", None
    return fake_resolve


# =========================================================================== #
# (1) NON-DOPPIA-RISOLUZIONE — claim esclusivo tra DUE sessioni interlacciate
# =========================================================================== #
@pytest.mark.asyncio
async def test_claim_is_exclusive_across_two_sessions_no_double():
    """Due sessioni/account che pescano dalla STESSA lista, alternati fino a esaurimento,
    non ricevono MAI lo stesso row id, e insieme coprono TUTTA la lista (nessuna persa)."""
    N = 6
    cid = await _mk_import_campaign(n_pending=N)

    async with AsyncSessionLocal() as dbA, AsyncSessionLocal() as dbB:
        claimed = []
        # Interlacciamento deterministico: A poi B, round dopo round, finche' entrambi None.
        for _ in range(N + 2):
            ra = await bi.claim_next_pending_import(dbA, cid, "acc-A")
            rb = await bi.claim_next_pending_import(dbB, cid, "acc-B")
            if ra is not None:
                assert ra.status == "resolving"
                claimed.append(ra.id)
            if rb is not None:
                assert rb.status == "resolving"
                claimed.append(rb.id)
            if ra is None and rb is None:
                break

    # Nessun id preso due volte + copertura completa della lista.
    assert len(claimed) == N, f"attesi {N} claim, visti {len(claimed)}"
    assert len(set(claimed)) == N, "un ImportedProfile e' stato claimato da entrambi (doppia risoluzione!)"

    async with AsyncSessionLocal() as db:
        # Tutte le righe ora sono 'resolving' (claimate), zero 'pending' residue.
        resolving = await db.scalar(
            select(func.count()).select_from(ImportedProfile).where(
                ImportedProfile.campaign_id == cid, ImportedProfile.status == "resolving"))
        pending = await db.scalar(
            select(func.count()).select_from(ImportedProfile).where(
                ImportedProfile.campaign_id == cid, ImportedProfile.status == "pending"))
        assert resolving == N and pending == 0


# =========================================================================== #
# (1) La FINESTRA DI RACE del claim: due sessioni SELECTano la STESSA riga pending,
#     poi entrambe tentano lo status-flip guardato -> UNA sola vince (rowcount==1).
# =========================================================================== #
@pytest.mark.asyncio
async def test_guarded_flip_only_one_winner_on_same_row():
    """Riproduzione deterministica della finestra SELECT->UPDATE interlacciata (cio' che
    claim_next_pending_import ritenta in loop): con UNA sola riga pending, se due sessioni
    la vedono entrambe 'pending', solo il primo UPDATE guardato (status='pending'->'resolving')
    fa rowcount==1; il secondo fa rowcount==0 (nessuna doppia risoluzione). Il perdente,
    come nel loop reale, ri-SELECTa e non trova piu' nulla."""
    cid = await _mk_import_campaign(n_pending=1)

    async with AsyncSessionLocal() as dbA, AsyncSessionLocal() as dbB:
        # Entrambe vedono la STESSA riga pending (finestra di race aperta).
        rowA = (await dbA.execute(select(ImportedProfile).where(
            ImportedProfile.campaign_id == cid, ImportedProfile.status == "pending").limit(1))).scalar_one()
        rowB = (await dbB.execute(select(ImportedProfile).where(
            ImportedProfile.campaign_id == cid, ImportedProfile.status == "pending").limit(1))).scalar_one()
        assert rowA.id == rowB.id  # la race e' reale: stessa riga

        winA = await dbA.execute(update(ImportedProfile).where(
            ImportedProfile.id == rowA.id, ImportedProfile.status == "pending"
        ).values(status="resolving"))
        await dbA.commit()

        winB = await dbB.execute(update(ImportedProfile).where(
            ImportedProfile.id == rowB.id, ImportedProfile.status == "pending"
        ).values(status="resolving"))
        await dbB.commit()

        assert winA.rowcount == 1, "il primo claim deve vincere"
        assert winB.rowcount == 0, "il secondo NON deve poter claimare la stessa riga (doppia risoluzione!)"

        # Il perdente ri-SELECTa (come il retry del loop reale): nessun pending residuo.
        again = (await dbB.execute(select(ImportedProfile).where(
            ImportedProfile.campaign_id == cid, ImportedProfile.status == "pending").limit(1))).scalar_one_or_none()
        assert again is None


# =========================================================================== #
# (1)+(2)+(3) DUE mini-sessioni condividono il pool: no doppioni, nessuna persa, 1 completamento
# =========================================================================== #
@pytest.mark.asyncio
async def test_two_sessions_share_pool_exactly_once(monkeypatch):
    """acc-A e acc-B risolvono la STESSA lista (cap piccolo -> entrambi lavorano davvero).
    Prova: N Follower, N righe resolved, 0 duplicati, campagna 'ready' UNA sola volta
    (scrape_complete emesso esattamente 1)."""
    N = 6
    CAP = 3
    cid = await _mk_import_campaign(n_pending=N, messaging=True)
    _use_fake_browser(monkeypatch, cap=CAP)
    events = _capture_events(monkeypatch)
    monkeypatch.setattr(bi, "resolve_and_store_bio_browser", _faithful_resolve_factory())

    # Sequenziali-ma-condividendo-il-pool: A prende cap righe, B le restanti e completa.
    defer_a = await bi.resolve_imports_browser_session(cid, "acc-A")
    defer_b = await bi.resolve_imports_browser_session(cid, "acc-B")

    # A non completa (pool non vuoto): defer di pausa. B drena e completa: None.
    assert isinstance(defer_a, int) and defer_a >= 60
    assert defer_b is None

    async with AsyncSessionLocal() as db:
        followers = (await db.execute(
            select(Follower).where(Follower.campaign_id == cid))).scalars().all()
        pks = [f.ig_user_id for f in followers]
        # (1) nessun profilo risolto due volte -> nessun Follower duplicato
        assert len(followers) == N
        assert len(set(pks)) == N, f"Follower duplicati: {pks}"

        # (2) nessuna riga persa: tutte terminali 'resolved'
        resolved = await db.scalar(
            select(func.count()).select_from(ImportedProfile).where(
                ImportedProfile.campaign_id == cid, ImportedProfile.status == "resolved"))
        open_rows = await db.scalar(
            select(func.count()).select_from(ImportedProfile).where(
                ImportedProfile.campaign_id == cid,
                ImportedProfile.status.in_(("pending", "resolving"))))
        assert resolved == N and open_rows == 0

        # (3) campagna completata una volta sola, coerente
        c = await db.get(Campaign, cid)
        assert c.status == CampaignStatus.ready
        assert c.total_followers == N
        assert c.messages_pending == N
        assert c.scrape_outcome == "completed"

    # (3) evento di completamento emesso ESATTAMENTE una volta (una sola transizione vince)
    n_complete = sum(1 for (_cid, action, _lvl) in events if action == "scrape_complete")
    assert n_complete == 1, f"scrape_complete emesso {n_complete} volte (atteso 1)"


# =========================================================================== #
# (3) La transizione di completamento avviene ESATTAMENTE una volta (guard atomico)
# =========================================================================== #
@pytest.mark.asyncio
async def test_complete_transitions_exactly_once(monkeypatch):
    """Con pool drenato (tutte terminali), la PRIMA _complete_import_browser vince
    (scraping->ready), la SECONDA no (campagna non piu' in _RESOLVING): mai doppia completione."""
    events = _capture_events(monkeypatch)
    cid = await _mk_import_campaign(n_pending=0, messaging=True)
    # Due righe gia' terminali (nessuna pending/resolving): pool "drenato".
    async with AsyncSessionLocal() as db:
        db.add(ImportedProfile(campaign_id=cid, raw_input="a", username="a", status="resolved", ig_user_id=1))
        db.add(ImportedProfile(campaign_id=cid, raw_input="b", username="b", status="not_found"))
        db.add(Follower(campaign_id=cid, ig_user_id=1, username="a", status=FollowerStatus.bio_scraped))
        await db.commit()

    first = await bi._complete_import_browser(cid)
    second = await bi._complete_import_browser(cid)
    assert first is True and second is False

    async with AsyncSessionLocal() as db:
        c = await db.get(Campaign, cid)
        assert c.status == CampaignStatus.ready
    assert sum(1 for (_c, a, _l) in events if a == "scrape_complete") == 1


# =========================================================================== #
# (4) STALE-RECOVERY end-to-end: 'resolving' orfana recuperata e risolta, NON persa
# =========================================================================== #
@pytest.mark.asyncio
async def test_stale_resolving_recovered_end_to_end(monkeypatch):
    """Una riga rimasta 'resolving' da una sessione MORTA (updated_at oltre il timeout) viene
    recuperata dal claim, risolta e trasformata in Follower: nessuna riga persa nel fan-out."""
    cid = await _mk_import_campaign(n_pending=0, messaging=False)
    async with AsyncSessionLocal() as db:
        old = datetime.utcnow() - timedelta(hours=2)
        db.add(ImportedProfile(campaign_id=cid, raw_input="u7", username="u7",
                               status="resolving", updated_at=old))
        await db.commit()

    _use_fake_browser(monkeypatch, cap=5)
    _capture_events(monkeypatch)
    monkeypatch.setattr(bi, "resolve_and_store_bio_browser", _faithful_resolve_factory())

    defer = await bi.resolve_imports_browser_session(cid, "acc-recover")
    assert defer is None  # pool drenato -> completa

    async with AsyncSessionLocal() as db:
        r = (await db.execute(select(ImportedProfile).where(
            ImportedProfile.campaign_id == cid))).scalar_one()
        assert r.status == "resolved", "la 'resolving' orfana non e' stata recuperata (riga persa!)"
        n = await db.scalar(select(func.count()).select_from(Follower).where(
            Follower.campaign_id == cid))
        assert n == 1
        c = await db.get(Campaign, cid)
        assert c.status == CampaignStatus.completed  # messaging=False


# =========================================================================== #
# (4b) Una 'resolving' FRESCA (sessione viva lenta) NON viene rubata dal claim
# =========================================================================== #
@pytest.mark.asyncio
async def test_fresh_resolving_not_stolen():
    """Solo la 'resolving' STALE viene recuperata; quella FRESCA (updated_at=now) resta
    intoccata: nessuna doppia risoluzione di una riga ancora in carico a una sessione viva."""
    cid = await _mk_import_campaign(n_pending=0)
    async with AsyncSessionLocal() as db:
        fresh = ImportedProfile(campaign_id=cid, raw_input="fresh", username="fresh",
                                status="resolving", updated_at=datetime.utcnow())
        stale = ImportedProfile(campaign_id=cid, raw_input="stale", username="stale",
                                status="resolving", updated_at=datetime.utcnow() - timedelta(hours=2))
        db.add(fresh)
        db.add(stale)
        await db.commit()
        fresh_id, stale_id = fresh.id, stale.id

    async with AsyncSessionLocal() as db:
        claimed = await bi.claim_next_pending_import(db, cid, "acc-thief")
        # recupera SOLO la stale, e la claima
        assert claimed is not None and claimed.id == stale_id

    async with AsyncSessionLocal() as db:
        f = await db.get(ImportedProfile, fresh_id)
        assert f.status == "resolving", "una 'resolving' FRESCA e' stata rubata a una sessione viva!"
        # e non c'e' piu' altro pending da claimare
        again = await bi.claim_next_pending_import(db, cid, "acc-thief")
        assert again is None


# =========================================================================== #
# (5) NO-PREMATURE-COMPLETE: finche' una riga e' 'resolving', la campagna resta scraping
# =========================================================================== #
@pytest.mark.asyncio
async def test_no_premature_completion_while_resolving(monkeypatch):
    """Con una riga ancora 'resolving' (in volo su un altro account), _complete_import_browser
    ritorna False e la campagna NON transiziona. Quando la riga diventa terminale, completa."""
    events = _capture_events(monkeypatch)
    cid = await _mk_import_campaign(n_pending=0, messaging=True)
    async with AsyncSessionLocal() as db:
        # una risolta (con Follower) + una ancora in volo 'resolving'
        db.add(ImportedProfile(campaign_id=cid, raw_input="done", username="done",
                               status="resolved", ig_user_id=42))
        db.add(Follower(campaign_id=cid, ig_user_id=42, username="done", status=FollowerStatus.bio_scraped))
        inflight = ImportedProfile(campaign_id=cid, raw_input="wip", username="wip",
                                   status="resolving", updated_at=datetime.utcnow())
        db.add(inflight)
        await db.commit()
        inflight_id = inflight.id

    # NON deve completare: c'e' ancora una 'resolving' aperta.
    assert await bi._complete_import_browser(cid) is False
    async with AsyncSessionLocal() as db:
        c = await db.get(Campaign, cid)
        assert c.status == CampaignStatus.scraping  # invariato
    assert sum(1 for (_c, a, _l) in events if a == "scrape_complete") == 0

    # L'ultimo account chiude la sua riga -> ora il pool e' vuoto -> completa (una volta).
    async with AsyncSessionLocal() as db:
        r = await db.get(ImportedProfile, inflight_id)
        r.status = "resolved"
        r.ig_user_id = 43
        db.add(Follower(campaign_id=cid, ig_user_id=43, username="wip", status=FollowerStatus.bio_scraped))
        await db.commit()

    assert await bi._complete_import_browser(cid) is True
    async with AsyncSessionLocal() as db:
        c = await db.get(Campaign, cid)
        assert c.status == CampaignStatus.ready
        assert c.total_followers == 2
    assert sum(1 for (_c, a, _l) in events if a == "scrape_complete") == 1
