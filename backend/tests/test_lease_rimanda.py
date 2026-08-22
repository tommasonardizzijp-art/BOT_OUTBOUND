"""Task 6: la prenotazione persa rimanda il lead, non lo scarta per sempre.

`contact_reservations` (reservation.py) e' un lease di 30 minuti, rilasciato
subito dopo l'invio riuscito. Prima di questo lavoro, chi perdeva la corsa
(`reservation.try_reserve` -> False) veniva marcato `FollowerStatus.skipped`
con `skip_reason="already_contacted_globally"` -- definitivo: quel lead non
veniva mai piu' ripreso. Un lock temporaneo produceva uno scarto permanente,
e il nome descriveva un blocco cross-campagna che non e' mai esistito
(`_legacy_global_contact_placeholder`, mai chiamato da nessuno -- rimosso in
questa stessa modifica).

Dopo la Task 5 (targa provvisoria ammessa in anagrafica), l'unico motivo per
cui si arriva qui e' che un ALTRO worker sta davvero lavorando quel contatto
in questo momento: la prenotazione persa non e' piu' un rifiuto per targa
"di serie B", e' vera contesa.

Rischio verificato in questo file, non solo dedotto: se il follower viene
solo sbloccato (nessun lock, nessuno stato terminale) il `while True` del
worker lo riclaimerebbe SUBITO -- stesso identico pattern di hot-loop gia'
documentato in test_campaign_orchestrator_browser_busy.py per
AccountBrowserBusy. Qui la protezione e' un backoff che cresce (stessa
`_gen_backoff_seconds` gia' usata per i 429 dell'AI) e, dopo
LEASE_LOST_DEFER_THRESHOLD perdite di fila, un vero `Retry(defer=...)` che fa
uscire il job invece di continuare a spendere round-trip DB a vuoto.
"""
import uuid
from datetime import datetime, timedelta

import pytest
from arq.worker import Retry

from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount, AccountStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.campaign_account import CampaignAccount
from app.models.follower import Follower, FollowerStatus
from app.services import campaign_orchestrator


class _FakeSessionManager:
    """Nessun break, sempre orario attivo -- il worker arriva dritto al claim."""
    def is_active_hour(self):
        return True

    def should_break_session(self):
        return False


async def _setup_campaign_account_follower():
    # base unico per run (vedi stesso pattern/motivazione in
    # test_campaign_orchestrator_browser_busy.py::test_setup_helper_non_collide...).
    base = 991_000_000_000 + uuid.uuid4().int % 100_000_000
    acc_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        db.add(InstagramAccount(
            id=acc_id, username=f"acc_lease_{base}", encrypted_password="x",
            status=AccountStatus.active, daily_message_limit=20,
            created_at=datetime.utcnow() - timedelta(days=100),
        ))
        camp = Campaign(name="t-lease", status=CampaignStatus.running, source_type="scrape")
        db.add(camp)
        await db.flush()
        db.add(CampaignAccount(campaign_id=camp.id, account_id=acc_id))
        follower = Follower(
            campaign_id=camp.id, ig_user_id=base, username=f"u{base}",
            status=FollowerStatus.message_generated,
        )
        db.add(follower)
        await db.flush()
        await db.commit()
        return camp.id, acc_id, follower.id


def _patch_common(monkeypatch):
    async def _not_halted(db):
        return False

    monkeypatch.setattr(campaign_orchestrator, "is_halted", _not_halted)
    monkeypatch.setattr(campaign_orchestrator, "SessionManager", _FakeSessionManager)
    monkeypatch.setattr(campaign_orchestrator, "ensure_campaign_can_send_messages", lambda campaign: None)
    # Azzera lo stagger di avvio (0-10s reali) -- non e' cio' che il test misura.
    monkeypatch.setattr("random.uniform", lambda a, b: 0)


@pytest.mark.asyncio
async def test_lease_persa_non_scarta_il_follower(monkeypatch):
    """Criterio d'accettazione dato dal piano: dopo una prenotazione persa,
    il follower NON e' skipped e resta ripescabile (nessun lock appeso)."""
    campaign_id, account_id, follower_id = await _setup_campaign_account_follower()
    _patch_common(monkeypatch)

    async def _try_reserve_sempre_perso(ig_user_id, owner_job, campaign_id, db):
        return False

    monkeypatch.setattr(campaign_orchestrator.reservation, "try_reserve", _try_reserve_sempre_perso)

    sleeps: list[float] = []

    async def _fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(campaign_orchestrator.asyncio, "sleep", _fake_sleep)

    with pytest.raises(Retry):
        await campaign_orchestrator.run_campaign_worker(campaign_id, account_id)

    async with AsyncSessionLocal() as db:
        follower = await db.get(Follower, follower_id)
        # Criterio del piano, testuale.
        assert follower.status != FollowerStatus.skipped
        assert follower.locked_by_account_id is None
        # Non deve MAI passare per lo scarto "definitivo" di prima di questa task.
        assert follower.skip_reason != "already_contacted_globally"

    # Prova ESEGUITA (non dedotta) che il worker non ha fatto hot-loop a delay
    # zero: ha effettivamente backoffato prima di ogni re-claim, con un
    # ritardo crescente (stesso schema di _gen_backoff_seconds).
    assert len(sleeps) == campaign_orchestrator.LEASE_LOST_DEFER_THRESHOLD - 1
    assert all(s > 0 for s in sleeps)
    assert sleeps == sorted(sleeps)  # cresce (o resta uguale al tetto), non decresce


@pytest.mark.asyncio
async def test_lease_persa_ripetuta_defer_invece_di_girare_a_vuoto(monkeypatch):
    """Prova diretta del rischio ciclo infinito segnalato in review: con un
    solo follower disponibile e la prenotazione SEMPRE persa, il worker deve
    uscire con un Retry (job rimandato) dopo LEASE_LOST_DEFER_THRESHOLD
    tentativi -- non continuare a riclaimare lo stesso follower all'infinito.
    Se questo test impiegasse piu' di qualche secondo reale (asyncio.sleep
    patchato via _fake_sleep sopra) o non sollevasse mai Retry, il worker
    starebbe girando a vuoto."""
    campaign_id, account_id, follower_id = await _setup_campaign_account_follower()
    _patch_common(monkeypatch)

    chiamate = {"n": 0}

    async def _try_reserve_sempre_perso(ig_user_id, owner_job, campaign_id, db):
        chiamate["n"] += 1
        return False

    monkeypatch.setattr(campaign_orchestrator.reservation, "try_reserve", _try_reserve_sempre_perso)
    monkeypatch.setattr(campaign_orchestrator.asyncio, "sleep", lambda *_a, **_k: _NoopAwaitable())

    with pytest.raises(Retry) as exc_info:
        await campaign_orchestrator.run_campaign_worker(campaign_id, account_id)

    # Esattamente la soglia: non una in meno (avrebbe girato ancora), non una
    # in piu' (la soglia non avrebbe fatto scattare il defer).
    assert chiamate["n"] == campaign_orchestrator.LEASE_LOST_DEFER_THRESHOLD
    # E' il defer di sessione (minuti), non un rinvio breve tipo AccountBrowserBusy:
    # qui il costo e' voluto (il pool e' contesissimo), diverso da un errore tecnico.
    assert exc_info.value.defer_score > 0


@pytest.mark.asyncio
async def test_prova_del_nove_senza_soglia_il_worker_girerebbe_a_vuoto(monkeypatch):
    """Se questo test PASSA anche disattivando la soglia di defer, il test
    sopra non starebbe provando nulla (stesso schema di
    test_worker_prova_del_nove_senza_except_busy_marca_fallito in
    test_campaign_orchestrator_browser_busy.py). Alziamo la soglia a un
    numero enorme (mai raggiunta) e mettiamo un tetto ai tentativi SOLO per
    non far girare il test reale all'infinito: se il worker raggiunge quel
    tetto senza mai sollevare Retry, e' la prova diretta che senza la soglia
    il ciclo `while True` avrebbe continuato a riclaimare lo stesso follower
    a raffica, esattamente il rischio segnalato in review."""
    campaign_id, account_id, follower_id = await _setup_campaign_account_follower()
    _patch_common(monkeypatch)
    monkeypatch.setattr(campaign_orchestrator, "LEASE_LOST_DEFER_THRESHOLD", 10**9)
    monkeypatch.setattr(campaign_orchestrator.asyncio, "sleep", lambda *_a, **_k: _NoopAwaitable())

    TETTO_TEST = 30  # >> soglia reale (3): se ci arriva, il ciclo non si fermerebbe mai da solo
    chiamate = {"n": 0}

    async def _try_reserve_sempre_perso(ig_user_id, owner_job, campaign_id, db):
        chiamate["n"] += 1
        if chiamate["n"] >= TETTO_TEST:
            raise _GiroAVuoto(chiamate["n"])
        return False

    monkeypatch.setattr(campaign_orchestrator.reservation, "try_reserve", _try_reserve_sempre_perso)

    with pytest.raises(_GiroAVuoto):
        await campaign_orchestrator.run_campaign_worker(campaign_id, account_id)

    assert chiamate["n"] == TETTO_TEST


class _GiroAVuoto(Exception):
    """Sentinella usata solo dalla prova del nove per fermare in modo
    controllato un ciclo che, senza la soglia di defer, non si fermerebbe."""


class _NoopAwaitable:
    def __await__(self):
        return iter(())


def test_codice_morto_rimosso():
    """Step 4 del piano: i due placeholder del blocco permanente cross-
    campagna (mai chiamati da nessuno -- verificato con grep su app/ e
    tests/, solo le due `def` comparivano) non esistono piu'. Lasciarli
    lì faceva concludere a chi legge che quella protezione esistesse."""
    assert not hasattr(campaign_orchestrator, "_legacy_global_contact_placeholder")
    assert not hasattr(campaign_orchestrator, "_legacy_release_placeholder")
