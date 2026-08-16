"""Parte B del cantiere skew: i confronti fra "adesso" e una colonna
`DateTime(timezone=True)`.

**Perche' questi test sono scritti in modo strano.** La suite gira su SQLite,
che per `DateTime(timezone=True)` restituisce sempre datetime *naive*: un
confronto naive-vs-naive non solleva mai e uno scarto di due ore non si vede.
Su PostgreSQL la stessa colonna torna *aware* e lo stesso codice esplode con
`TypeError: can't compare offset-naive and offset-aware datetimes` -- e' cosi'
che il 16/08 un 409 e' diventato un 500. Passare qui non e' evidenza di
niente, se il test si limita a rifare il giro del DB.

Quindi i test non si appoggiano al round-trip, e usano due oracoli
indipendenti dal codice sotto esame:

1. per i confronti in Python, l'input aware si **costruisce a mano**, com'e'
   quello che PostgreSQL consegnerebbe;
2. per i confronti che avvengono in SQL, si legge il **parametro davvero
   legato alla query** (fixture `istanti_legati`): li' un naive non da'
   errore su nessun backend, ma su una colonna `timestamptz` PostgreSQL lo
   interpreta come ora locale e sposta la soglia di tutto l'offset del fuso.
   E' l'unico punto in cui quel difetto e' osservabile da SQLite.
"""
import contextlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.utils.tempo import adesso_utc, as_utc

# Un fuso qualunque diverso da UTC: serve a provare che as_utc CONVERTE e non
# si limita ad accettare qualunque cosa abbia un tzinfo.
ROMA_ESTATE = timezone(timedelta(hours=2))


@pytest.fixture
def istanti_legati():
    """Ogni datetime che finisce come parametro di una query, su qualunque
    engine.

    Si aggancia a `before_execute`, dove la clausola e' ancora un albero
    SQLAlchemy: compilarla restituisce i letterali immersi nel `where`, cioe'
    esattamente il valore che il driver spedirebbe a PostgreSQL. E' un oracolo
    che guarda la QUERY e non il codice che l'ha costruita: continua a
    funzionare anche se domani quel codice cambia strada.
    """
    registrati: list[tuple[str, datetime]] = []

    def _cattura(conn, clause, multiparams, params, execution_options):
        with contextlib.suppress(Exception):
            for nome, valore in (clause.compile().params or {}).items():
                if isinstance(valore, datetime):
                    registrati.append((nome, valore))

    event.listen(Engine, "before_execute", _cattura)
    try:
        yield registrati
    finally:
        event.remove(Engine, "before_execute", _cattura)


def _naive_fra(registrati) -> list[str]:
    return [f"{nome}={valore!r}" for nome, valore in registrati
            if valore.tzinfo is None]


def _verifica_binding_aware(registrati) -> None:
    # La lista vuota e' il modo silenzioso in cui questo test morirebbe: se la
    # query non e' mai partita, "nessun naive" e' vero e non significa nulla.
    assert registrati, ("nessun istante e' finito in una query: la funzione "
                        "sotto esame non ha interrogato il DB, il test non "
                        "sta guardando niente")
    assert not _naive_fra(registrati), (
        "istanti NAIVE legati a una query su colonne timestamptz: PostgreSQL "
        "li legge come ora locale e sposta la soglia dell'offset del fuso "
        "(-2h in ora legale).\n  " + "\n  ".join(_naive_fra(registrati)))


async def _scenario_pronto(db_session, *, minuti_fa: int = 1):
    """Campagna running con UNA riga eleggibile, scritta con istanti AWARE.

    Gli istanti sono espliciti e aware apposta: le factory condivise usano
    ancora `datetime.utcnow()` e scriverebbero naive, cioe' proprio cio' che
    questi test devono escludere dai dati di partenza.
    """
    from app.models.wa import (WaCampaignContact, WaCampaignStatus,
                               WaContactStatus, WaSendCondition, WaSequenceStep)
    from tests.factories_wa import make_campaign, make_contact, make_number, make_tenant

    tenant = await make_tenant(db_session)
    number = await make_number(db_session, tenant)
    campaign, _step = await make_campaign(db_session, tenant, number,
                                          status=WaCampaignStatus.running)
    campaign.started_at = adesso_utc()
    contact = await make_contact(db_session, tenant)
    cc = WaCampaignContact(
        id=str(uuid.uuid4()), campaign_id=campaign.id, contact_id=contact.id,
        status=WaContactStatus.queued, current_step=-1, failure_count=0,
        next_action_at=datetime.now(timezone.utc) - timedelta(minutes=minuti_fa))
    db_session.add(cc)
    await db_session.commit()
    return {"tenant": tenant, "number": number, "campaign": campaign,
            "contact": contact, "cc": cc}


# ===========================================================================
# Gli helper: l'unico posto in cui "adesso" e "questo istante dal DB" nascono
# ===========================================================================

def test_adesso_utc_e_aware_e_indica_l_istante_giusto():
    """L'atteso NON viene da datetime: `time.time()` e' una strada
    indipendente per lo stesso istante, altrimenti il test riuserebbe la
    funzione che deve giudicare e non potrebbe fallire."""
    import time

    quando = adesso_utc()
    assert quando.tzinfo is not None, "naive: e' esattamente il difetto"
    assert quando.utcoffset() == timedelta(0)
    assert abs(quando.timestamp() - time.time()) < 5


def test_as_utc_normalizza_il_naive_e_converte_l_aware():
    atteso = datetime(2026, 8, 16, 13, 5, 21, tzinfo=timezone.utc)

    # Naive: si assume gia' UTC (e' cio' che il canale scrive ovunque), non si
    # reinterpreta come ora locale -- che sarebbe il difetto, al contrario.
    assert as_utc(datetime(2026, 8, 16, 13, 5, 21)) == atteso
    # Aware in un altro fuso: si converte davvero.
    assert as_utc(datetime(2026, 8, 16, 15, 5, 21, tzinfo=ROMA_ESTATE)) == atteso
    assert as_utc(datetime(2026, 8, 16, 15, 5, 21, tzinfo=ROMA_ESTATE)).utcoffset() \
        == timedelta(0)
    assert as_utc(None) is None


# ===========================================================================
# Confronto in Python: l'unico del canale, ed e' quello che sollevava
# ===========================================================================

def test_lock_fresco_regge_il_locked_at_aware_di_postgres():
    """`_lock_fresco` confronta in Python, non in SQL: e' la forma esatta del
    500 del 16/08. Deve reggere l'aware (PostgreSQL) e il naive (SQLite) senza
    cambiare risposta -- lo stesso lock non puo' essere "fresco" su un backend
    e "stale" sull'altro."""
    from app.api.wa_contacts import _lock_fresco
    from app.config import settings
    from app.models.wa import WaCampaignContact

    scaduto = int(settings.wa_lock_timeout_min) + 5

    def riga(locked_at):
        return WaCampaignContact(id="x", campaign_id="c", contact_id="k",
                                 locked_by="worker-1", locked_at=locked_at)

    adesso = datetime.now(timezone.utc)
    # Aware: cio' che restituisce PostgreSQL.
    assert _lock_fresco(riga(adesso - timedelta(minutes=1))) is True
    assert _lock_fresco(riga(adesso - timedelta(minutes=scaduto))) is False
    # Aware in un fuso non-UTC: stesso istante, stessa risposta.
    assert _lock_fresco(riga((adesso - timedelta(minutes=1)).astimezone(ROMA_ESTATE))) \
        is True
    # Naive: cio' che restituisce SQLite, dove gira la suite.
    naive = adesso.replace(tzinfo=None)
    assert _lock_fresco(riga(naive - timedelta(minutes=1))) is True
    assert _lock_fresco(riga(naive - timedelta(minutes=scaduto))) is False
    # Nessun lock: il ramo che non arriva mai al confronto.
    assert _lock_fresco(riga(None)) is False


# ===========================================================================
# Confronti in SQL: quelli che girano a ogni tick del worker di invio
# ===========================================================================

@pytest.mark.asyncio
async def test_claim_next_wa_contact_lega_istanti_aware(db_session, istanti_legati):
    from app.workers import wa_worker

    ctx = await _scenario_pronto(db_session)
    istanti_legati.clear()   # da qui in poi gira solo codice di produzione

    esito = await wa_worker.claim_next_wa_contact(
        db_session, number_id=ctx["number"].id, worker_id="w-1")

    assert esito is not None, "la riga eleggibile non e' stata claimata"
    _verifica_binding_aware(istanti_legati)


@pytest.mark.asyncio
async def test_precheck_prima_del_browser_lega_istanti_aware(
        db_session, istanti_legati, monkeypatch):
    from app.workers import wa_worker

    ctx = await _scenario_pronto(db_session)
    monkeypatch.setattr(wa_worker, "_ora_locale_corrente", lambda: 11)
    istanti_legati.clear()

    # None = "c'e' lavoro, apri pure il browser". Un istante sbagliato di due
    # ore farebbe tornare 'niente_da_fare' su una campagna che ha lavoro.
    assert await wa_worker._niente_da_fare_prima_del_browser(ctx["number"].id) is None
    _verifica_binding_aware(istanti_legati)


@pytest.mark.asyncio
async def test_healthcheck_lega_un_cutoff_aware_sui_lock(
        db_session, istanti_legati, monkeypatch):
    from app.models.wa import WaNumberStatus
    from app.workers import cron_worker

    ctx = await _scenario_pronto(db_session)
    ctx["cc"].locked_by = "worker-morto"
    ctx["cc"].locked_at = datetime.now(timezone.utc) - timedelta(minutes=45)
    await db_session.commit()

    # `held` fa un create_pool VERO: senza un Redis vivo il test aspetterebbe
    # ~50s prima di fallire (stessa ragione di _lock_profilo_libero in
    # test_wa_cron.py). `check_session` aprirebbe un browser.
    @contextlib.asynccontextmanager
    async def _lucchetto_libero(number_id, *, ttl_min=None):
        yield "token-di-test"

    async def _sessione_viva(number_id):
        return WaNumberStatus.active

    monkeypatch.setattr(cron_worker.wa_profile_lock, "held", _lucchetto_libero)
    monkeypatch.setattr(cron_worker, "check_session", _sessione_viva)
    istanti_legati.clear()

    await cron_worker.wa_session_healthcheck({})

    await db_session.refresh(ctx["cc"])
    assert ctx["cc"].locked_by is None, "il lock stale non e' stato rilasciato"
    _verifica_binding_aware(istanti_legati)


@pytest.mark.asyncio
async def test_supervisore_lega_istanti_aware(db_session, istanti_legati, monkeypatch):
    from app.workers import cron_worker

    ctx = await _scenario_pronto(db_session)
    accodate: list[str] = []

    async def _finta_enqueue(campaign_id: str, **kw) -> int:
        accodate.append(campaign_id)
        return 1
    monkeypatch.setattr("app.workers.wa_worker.enqueue_wa_workers", _finta_enqueue)
    istanti_legati.clear()

    await cron_worker.wa_campaign_supervisor({})

    assert ctx["campaign"].id in accodate, (
        "il supervisore non ha visto la riga eleggibile")
    _verifica_binding_aware(istanti_legati)


@pytest.mark.asyncio
async def test_finestra_invii_recenti_lega_un_istante_aware(db_session, istanti_legati):
    """La finestra del reply-watcher e' un `sent_at >= adesso - N giorni`: con
    un istante naive PostgreSQL la sposta di due ore, e i numeri da
    scansionare cambiano proprio sul bordo della finestra."""
    from app.services import wa_reply_watcher

    await _scenario_pronto(db_session)
    istanti_legati.clear()

    numeri = await wa_reply_watcher.numeri_da_scansionare(db_session)

    assert isinstance(numeri, list)
    _verifica_binding_aware(istanti_legati)
