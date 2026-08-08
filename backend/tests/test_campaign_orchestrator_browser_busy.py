"""C.2/C.3 review: quando l'apertura del browser trova l'account occupato da
un'altra sessione (AccountBrowserBusy), il worker di campagna deve RINVIARE
il job (Retry) senza marcare il follower fallito e senza consumare i retry
verso l'auto-pausa della campagna. Senza questa traduzione la busy-exception
cadrebbe nel catch-all generico del loop (vedi campaign_orchestrator.py, il
blocco `except Exception as e:` che incrementa retry_count e puo' pausare la
campagna dopo 5 errori consecutivi) -- esattamente il rischio segnalato nella
review del piano C.1-C.3.

Due livelli di test:
1. `_release_after_browser_busy` in isolamento (fake follower/db/reservation),
   senza montare l'intero worker loop -- copre la logica di cleanup.
2. `run_campaign_worker` end-to-end con DB sqlite reale (stesso pattern di
   test_scrape_bios_browser_session.py: Campaign/InstagramAccount/Follower/
   Message costruiti a mano, BrowserSession sostituita da un fake che solleva
   AccountBrowserBusy) -- copre che il Retry arrivi davvero fuori dal loop
   intatto, non intercettato da un except piu' aggressivo lungo la strada.
"""
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from arq.worker import Retry

from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount, AccountStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.campaign_account import CampaignAccount
from app.models.follower import Follower, FollowerStatus
from app.models.message import Message, MessageStatus
from app.browser.profile_lock import AccountBrowserBusy, BUSY_RETRY_S
from app.services import campaign_orchestrator
import app.browser.context_manager as context_manager


# ─────────────────────── Livello 1: helper in isolamento ───────────────────────

class _FakeReservation:
    def __init__(self):
        self.released: list[int] = []

    async def release(self, ig_user_id, db):
        self.released.append(ig_user_id)


class _FakeDB:
    def __init__(self):
        self.committed = 0

    async def commit(self):
        self.committed += 1


@pytest.mark.asyncio
async def test_release_after_browser_busy_libera_follower_e_reservation(monkeypatch):
    follower = SimpleNamespace(locked_by_account_id="acc-1", locked_at=datetime.utcnow(), ig_user_id=4242)
    fake_reservation = _FakeReservation()
    monkeypatch.setattr(campaign_orchestrator, "reservation", fake_reservation)
    db = _FakeDB()

    defer = await campaign_orchestrator._release_after_browser_busy(
        follower, db, "camp-1", "acc-1", "owner-1", AccountBrowserBusy("occupato")
    )

    assert defer == BUSY_RETRY_S
    assert follower.locked_by_account_id is None
    assert follower.locked_at is None
    assert fake_reservation.released == [4242]
    assert db.committed >= 1


# ─────────────────────── Livello 2: worker end-to-end ───────────────────────

class _FakeSessionManager:
    """Sostituisce human_behavior.SessionManager: nessun break, sempre orario
    attivo, cosi' il worker arriva fino all'apertura del browser senza deviare."""
    def is_active_hour(self):
        return True

    def should_break_session(self):
        return False


class _FakeBusySession:
    """Sostituisce BrowserSession: apre e solleva subito AccountBrowserBusy,
    come farebbe la vera BrowserSession quando profile_lock.held_with_renew
    trova il profilo occupato da un altro processo."""
    def __init__(self, *a, **k):
        pass

    async def open(self):
        raise AccountBrowserBusy("profilo occupato (test)")


async def _setup_campaign_account_follower():
    base = 990_000_000_000 + int(datetime.utcnow().timestamp()) % 100_000
    acc_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        db.add(InstagramAccount(
            id=acc_id, username=f"acc_busy_{base}", encrypted_password="x",
            status=AccountStatus.active, daily_message_limit=20,
            # Account "vecchio" (100gg) e fuori dal warmup: senza questo
            # _get_effective_daily_limit applica il cap eta'-account (giorno
            # 0 = quasi zero DM/giorno per un account appena creato) e il
            # worker esce subito per "daily limit raggiunto (0/0)" prima
            # ancora di arrivare all'apertura del browser che si vuole testare.
            created_at=datetime.utcnow() - timedelta(days=100),
        ))
        camp = Campaign(name="t-busy", status=CampaignStatus.running, source_type="scrape")
        db.add(camp)
        await db.flush()
        db.add(CampaignAccount(campaign_id=camp.id, account_id=acc_id))
        follower = Follower(
            campaign_id=camp.id, ig_user_id=base, username=f"u{base}",
            status=FollowerStatus.message_generated,
        )
        db.add(follower)
        await db.flush()
        db.add(Message(
            campaign_id=camp.id, follower_id=follower.id,
            generated_text="ciao", status=MessageStatus.pending,
        ))
        await db.commit()
        return camp.id, acc_id, follower.id


@pytest.mark.asyncio
async def test_worker_rinvia_non_fallisce_quando_account_occupato(monkeypatch):
    campaign_id, account_id, follower_id = await _setup_campaign_account_follower()

    async def _not_halted(db):
        return False

    monkeypatch.setattr(campaign_orchestrator, "is_halted", _not_halted)
    monkeypatch.setattr(campaign_orchestrator, "SessionManager", _FakeSessionManager)
    monkeypatch.setattr(campaign_orchestrator, "ensure_campaign_can_send_messages", lambda campaign: None)
    # Azzera lo stagger di avvio (0-10s reali, random.uniform globale) — non
    # e' quello che il test vuole misurare, solo lo rallenta.
    monkeypatch.setattr("random.uniform", lambda a, b: 0)
    monkeypatch.setattr(context_manager, "BrowserSession", _FakeBusySession)

    with pytest.raises(Retry) as exc_info:
        await campaign_orchestrator.run_campaign_worker(campaign_id, account_id)

    # Rinviato con il defer breve dedicato, NON il break di fine sessione
    # (minuti-decine) e non un fallimento senza defer.
    assert exc_info.value.defer_score == BUSY_RETRY_S * 1000

    async with AsyncSessionLocal() as db:
        follower = await db.get(Follower, follower_id)
        campaign = await db.get(Campaign, campaign_id)
        msg_result = await db.execute(
            Message.__table__.select().where(Message.follower_id == follower_id)
        )
        message_row = msg_result.mappings().one()

        # Follower liberato, non marcato fallito.
        assert follower.locked_by_account_id is None
        assert follower.status == FollowerStatus.message_generated

        # Il messaggio non ha subito un retry/fallimento: nessuna penalita'
        # per un problema che non e' suo (il catch-all generico incrementa
        # retry_count e marca 'failed' dopo 3 tentativi -- qui deve restare 0).
        assert message_row["retry_count"] == 0
        assert message_row["status"] == MessageStatus.pending.value

        # La campagna resta viva: NON e' l'auto-pausa da errori consecutivi.
        assert campaign.status == CampaignStatus.running


@pytest.mark.asyncio
async def test_worker_prova_del_nove_senza_except_busy_marca_fallito(monkeypatch):
    """Prova del nove: se la traduzione AccountBrowserBusy -> Retry non ci
    fosse (il codice precedente alla review), la busy-exception cadrebbe nel
    catch-all generico e — dato che qui e' l'UNICO follower/messaggio della
    campagna, quindi non ci sono altri retry disponibili sullo stesso
    messaggio — verrebbe trattata come un errore inatteso qualsiasi:
    follower/reservation liberati SI', ma SENZA il Retry(defer=...) dedicato
    (nessuna garanzia di ripartenza breve) e con retry_count incrementato.
    Qui si dimostra rimuovendo l'except specifico via monkeypatch e
    verificando che il comportamento cambi (niente piu' Retry, retry_count
    incrementato) -- se questo test PASSA anche col except integro, il test
    sopra non starebbe provando nulla."""
    campaign_id, account_id, follower_id = await _setup_campaign_account_follower()

    async def _not_halted(db):
        return False

    class _FakeBusySessionAsGenericError:
        """Stesso fallimento ma sollevato come Exception generica pura, per
        simulare 'cosa succederebbe senza except AccountBrowserBusy dedicato'
        senza dover davvero rimuovere codice di produzione."""
        def __init__(self, *a, **k):
            pass

        async def open(self):
            raise RuntimeError("apertura fallita (simulazione: nessuna traduzione busy->Retry)")

    monkeypatch.setattr(campaign_orchestrator, "is_halted", _not_halted)
    monkeypatch.setattr(campaign_orchestrator, "SessionManager", _FakeSessionManager)
    monkeypatch.setattr(campaign_orchestrator, "ensure_campaign_can_send_messages", lambda campaign: None)
    # Azzera lo stagger di avvio (0-10s reali, random.uniform globale) — non
    # e' quello che il test vuole misurare, solo lo rallenta.
    monkeypatch.setattr("random.uniform", lambda a, b: 0)
    monkeypatch.setattr(context_manager, "BrowserSession", _FakeBusySessionAsGenericError)

    # Nessun Retry: il catch-all generico ingoia l'eccezione e ritorna None
    # (worker_error, non un raise). Senza un Retry(defer=...) che fa uscire
    # il job, il `while True:` esterno riclaima SUBITO lo stesso follower
    # (appena sbloccato) e ci riprova all'istante -- tre colpi a raffica
    # senza alcuna pausa, non un rinvio breve e controllato.
    await campaign_orchestrator.run_campaign_worker(campaign_id, account_id)

    async with AsyncSessionLocal() as db:
        msg_result = await db.execute(
            Message.__table__.select().where(Message.follower_id == follower_id)
        )
        message_row = msg_result.mappings().one()
        follower = await db.get(Follower, follower_id)
        # A differenza del path AccountBrowserBusy (retry_count resta 0,
        # follower riprovabile): qui il catch-all generico ha bruciato tutti
        # e 3 i tentativi nella STESSA invocazione (nessun defer fra un
        # colpo e l'altro) e ha marcato il follower DEFINITIVAMENTE fallito
        # -- un lead perso per un problema che non era suo. Questa e' la
        # prova che la traduzione dedicata non e' cosmetica.
        assert message_row["retry_count"] == 3
        assert message_row["status"] == MessageStatus.failed.value
        assert follower.status == FollowerStatus.failed
