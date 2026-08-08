"""Tetto giornaliero PERSISTITO ai like ambientali (browse_feed).

Un like e' una SCRITTURA (vettore di blocco proprio, peggiore dello scrape in
lettura): prima di questo modulo il tetto viveva solo in `max_likes`, una
variabile locale alla funzione azzerata a ogni chiamata di browse_feed -- cento
chiamate = cento budget nuovi. `account_manager.reserve_daily_like` e' la
prenotazione ATOMICA e persistita (migrazione 030) che sostituisce quel falso
limite; questo file prova le invarianti dichiarate nel piano B.3:

  1) tetto raggiunto => zero like, la sessione PROSEGUE (no eccezione, no
     return anticipato) -- test su InstagramPage.browse_feed;
  2) cambio di giorno => il contatore riparte da se' (reset lazy, come
     scrape_lookups);
  3) concorrenza VERA (asyncio.gather, sessioni DB separate) con tetto=1 =>
     esattamente una prenotazione passa;
  4) il contatore sopravvive a una sessione NUOVA (persistito, non in
     memoria come il vecchio max_likes locale);
  5) nessun like puo' scattare su un profilo target: browse_feed rinavativa
     sempre verso l'home feed prima di qualunque interazione.
"""
import asyncio
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.browser import instagram_page as ip_module
from app.browser.instagram_page import InstagramPage
from app.config import settings
from app.database import AsyncSessionLocal
from app.models.account import AccountStatus, InstagramAccount
from app.services import account_manager


def _today() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def _yesterday() -> str:
    return (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")


async def _make_account(db, **overrides) -> str:
    acc_id = str(uuid.uuid4())
    kwargs = dict(
        id=acc_id, username=f"acc_{acc_id[:8]}",
        encrypted_password="x", status=AccountStatus.active,
        daily_message_limit=20,
    )
    kwargs.update(overrides)
    db.add(InstagramAccount(**kwargs))
    await db.commit()
    return acc_id


async def _read_likes(acc_id: str):
    """Rilettura da una sessione NUOVA -- prova che il valore e' nel DB, non
    nella identity map della sessione che ha scritto."""
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(InstagramAccount).where(InstagramAccount.id == acc_id)
        )).scalar_one()
        return row.daily_likes_today, row.daily_likes_date


# ── 1) DB: reserve_daily_like, tetto/reset lazy/persistenza ─────────────────

@pytest.mark.asyncio
async def test_reserve_denied_when_cap_already_reached(db_session, monkeypatch):
    monkeypatch.setattr(account_manager.settings, "daily_like_cap", 2, raising=False)
    acc_id = await _make_account(db_session, daily_likes_today=2, daily_likes_date=_today())

    async with AsyncSessionLocal() as db:
        granted = await account_manager.reserve_daily_like(db, acc_id)

    assert granted is False
    likes, date = await _read_likes(acc_id)
    assert (likes, date) == (2, _today()), "tetto pieno: nessuna scrittura sporca"


@pytest.mark.asyncio
async def test_reserve_granted_when_under_cap(db_session, monkeypatch):
    monkeypatch.setattr(account_manager.settings, "daily_like_cap", 3, raising=False)
    acc_id = await _make_account(db_session, daily_likes_today=1, daily_likes_date=_today())

    async with AsyncSessionLocal() as db:
        granted = await account_manager.reserve_daily_like(db, acc_id)

    assert granted is True
    likes, date = await _read_likes(acc_id)
    assert (likes, date) == (2, _today())


@pytest.mark.asyncio
async def test_day_change_resets_counter_lazily(db_session, monkeypatch):
    """Contatore di IERI gia' al tetto (o oltre): oggi e' un giorno nuovo, la
    prenotazione riparte da se' senza dipendere da un cron di reset."""
    monkeypatch.setattr(account_manager.settings, "daily_like_cap", 2, raising=False)
    acc_id = await _make_account(db_session, daily_likes_today=99, daily_likes_date=_yesterday())

    async with AsyncSessionLocal() as db:
        granted = await account_manager.reserve_daily_like(db, acc_id)

    assert granted is True, "contatore di ieri deve leggersi come 0, non come 99"
    likes, date = await _read_likes(acc_id)
    assert (likes, date) == (1, _today())


@pytest.mark.asyncio
async def test_null_date_pre_migration_row_reads_as_zero(db_session, monkeypatch):
    """Riga pre-migrazione (date NULL, come le scrape_lookups_date storiche):
    trattata come stale => 0, non come 'mai inizializzata quindi bloccata'."""
    monkeypatch.setattr(account_manager.settings, "daily_like_cap", 1, raising=False)
    acc_id = await _make_account(db_session, daily_likes_today=0, daily_likes_date=None)

    async with AsyncSessionLocal() as db:
        granted = await account_manager.reserve_daily_like(db, acc_id)

    assert granted is True
    likes, date = await _read_likes(acc_id)
    assert (likes, date) == (1, _today())


@pytest.mark.asyncio
async def test_counter_survives_a_brand_new_session(db_session, monkeypatch):
    """Persistito, non in memoria: la prima prenotazione e la sua sessione DB
    si chiudono, una sessione TOTALMENTE nuova vede comunque il valore
    scritto -- e' esattamente il bug che questo modulo chiude (il vecchio
    max_likes locale a browse_feed si azzerava a ogni chiamata/riavvio)."""
    monkeypatch.setattr(account_manager.settings, "daily_like_cap", 2, raising=False)
    acc_id = await _make_account(db_session, daily_likes_today=0, daily_likes_date=_today())

    async with AsyncSessionLocal() as db1:
        assert await account_manager.reserve_daily_like(db1, acc_id) is True
    # db1 e' chiusa qui: nessuna sessione aperta porta con se' lo stato.

    async with AsyncSessionLocal() as db2:
        assert await account_manager.reserve_daily_like(db2, acc_id) is True
    likes, _ = await _read_likes(acc_id)
    assert likes == 2

    async with AsyncSessionLocal() as db3:
        # Tetto raggiunto dalle due sessioni precedenti (ORA CHIUSE): la terza,
        # ANCORA NUOVA, deve vederlo comunque -- non e' in memoria in nessun
        # processo/sessione vivente.
        assert await account_manager.reserve_daily_like(db3, acc_id) is False
    likes, _ = await _read_likes(acc_id)
    assert likes == 2, "la prenotazione negata non deve aver scritto nulla"


# ── 2) DB: concorrenza VERA, tetto=1 ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_concorrenza_reale_due_prenotazioni_stesso_account_tetto_1(db_session, monkeypatch):
    """Garanzia CENTRALE del modulo: due prenotazioni VERE (sessioni DB
    separate, asyncio.gather reale, non sequenziale) sullo stesso account con
    tetto=1 -- al piu' UNA deve passare. Un leggi-poi-scrivi (senza
    l'atomicita' della singola UPDATE condizionata) le farebbe passare
    entrambe: vedi la prova del nove riportata al team lead."""
    monkeypatch.setattr(account_manager.settings, "daily_like_cap", 1, raising=False)
    acc_id = await _make_account(db_session, daily_likes_today=0, daily_likes_date=_today())

    async def _reserve():
        async with AsyncSessionLocal() as db:
            return await account_manager.reserve_daily_like(db, acc_id)

    r1, r2 = await asyncio.gather(_reserve(), _reserve())

    assert sorted([r1, r2]) == [False, True], (
        f"esattamente una prenotazione doveva passare, esiti: {r1!r}, {r2!r}"
    )
    likes, _ = await _read_likes(acc_id)
    assert likes == 1, f"il contatore deve fermarsi ESATTAMENTE al tetto, non a {likes}"


# ── 3) InstagramPage.browse_feed: gate iniettato, comportamento al tetto ────

class _FakeMouse:
    def __init__(self):
        self.click_calls = 0
        self.wheel_calls = 0

    async def wheel(self, x, y):
        self.wheel_calls += 1

    async def move(self, x, y, steps=None):
        pass

    async def click(self, x, y):
        self.click_calls += 1


class _FakeLocator:
    def __init__(self, count, on_click=None):
        self._count = count
        self._on_click = on_click

    @property
    def first(self):
        return self

    async def count(self):
        return self._count

    async def click(self, timeout=None):
        if self._on_click:
            self._on_click()

    async def bounding_box(self):
        return {"x": 100.0, "y": 300.0, "width": 40.0, "height": 40.0}


class _FakePage:
    """`like_count`/`home_nav_count`/`post_count` instradati per selettore,
    come il codice reale di browse_feed (selettori diversi per like/home
    nav/post) -- una singola locator generica non basterebbe a distinguere i
    tre rami che il test deve controllare indipendentemente."""

    def __init__(
        self, start_url="https://www.instagram.com/", like_count=1, home_nav_count=0,
        raise_on_goto=False,
    ):
        self.mouse = _FakeMouse()
        self.url = start_url
        self.goto_calls = []
        self.home_nav_clicks = 0
        self._like_count = like_count
        self._home_nav_count = home_nav_count
        self._raise_on_goto = raise_on_goto

    def is_closed(self):
        return False

    def locator(self, selector):
        if "Like" in selector or "Mi piace" in selector:
            return _FakeLocator(self._like_count)
        if "Home" in selector or "Instagram" in selector:
            return _FakeLocator(self._home_nav_count, on_click=self._click_home)
        # link di post ("/p/", "/reel/"): mai aperto in questi test (max_posts=0)
        return _FakeLocator(0)

    def _click_home(self):
        self.home_nav_clicks += 1
        self.url = "https://www.instagram.com/"

    async def goto(self, url, wait_until=None):
        self.goto_calls.append(url)
        if self._raise_on_goto:
            # Simula una navigazione fallita (timeout/crash pagina): la
            # pagina resta ferma dove si trovava PRIMA -- vedi
            # test_browse_feed_navigation_failure_disables_likes_this_session.
            raise RuntimeError("navigation down")
        self.url = "https://www.instagram.com/"


class _FakeLoop:
    """Orologio monotono finto: ogni chiamata a .time() avanza di `step`.
    end_time e' fissato UNA volta (duration_seconds + primo .time()), quindi
    il while-loop di browse_feed termina da solo dopo un numero di iterazioni
    proporzionale a duration_seconds/step -- niente sleep reali, niente lista
    di valori precotta da azzeccare a mano."""

    def __init__(self, step=1.0):
        self._t = 0.0
        self._step = step

    def time(self):
        self._t += self._step
        return self._t


def _make_ig(page):
    ig = InstagramPage(context=None)
    ig._page = page
    return ig


def _patch_deterministic_loop(ig, monkeypatch, *, max_likes_choice, max_posts_choice=0):
    """Rende browse_feed deterministico: niente sleep reali, r fisso dentro
    la fascia 'like' (0.70<=r<0.78) a ogni iterazione, max_posts/max_likes
    scelti a comando invece che a caso."""
    # UNA sola istanza: asyncio.get_event_loop() e' chiamata piu' volte per
    # iterazione (while-check + remaining), deve sempre essere lo STESSO
    # orologio che avanza -- una lambda che ne crea una nuova ogni volta
    # azzererebbe .time() a ogni chiamata e il while non terminerebbe mai.
    fake_loop = _FakeLoop(step=1.0)
    monkeypatch.setattr(ip_module.asyncio, "get_event_loop", lambda: fake_loop)

    async def _fake_sleep(seconds):
        return None
    monkeypatch.setattr(ip_module.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(ip_module.random, "uniform", lambda a, b: a)

    def _fake_choice(seq):
        # random.choice([0, 0, 1, 1, 2]) per max_likes ha 5 elementi;
        # random.choice([0, 1, 1, 2]) per max_posts ne ha 4 -- si distinguono
        # dalla lunghezza, non serve altro.
        if len(seq) == 5:
            return max_likes_choice
        return max_posts_choice
    monkeypatch.setattr(ip_module.random, "choice", _fake_choice)

    calls = {"n": 0}

    def _fake_random():
        calls["n"] += 1
        if calls["n"] == 1:
            return 0.1  # like_target_prob = random.random() < 0.35 -> True
        return 0.76  # r di ogni iterazione: cade nel ramo "like" (0.70..0.78)
    monkeypatch.setattr(ip_module.random, "random", _fake_random)
    monkeypatch.setattr(ig, "_dismiss_ig_modals", lambda page, username: _noop())


async def _noop():
    return None


@pytest.mark.asyncio
async def test_browse_feed_cap_reached_zero_likes_session_continues(monkeypatch):
    """Invariante 1: gate che NEGA sempre => zero click, browse_feed ritorna
    normalmente (nessuna eccezione, nessun return anticipato -- se sollevasse
    o uscisse subito questo await non completerebbe)."""
    page = _FakePage(like_count=1, home_nav_count=0)
    ig = _make_ig(page)
    _patch_deterministic_loop(ig, monkeypatch, max_likes_choice=2)

    gate_calls = {"n": 0}

    async def _denied_gate() -> bool:
        gate_calls["n"] += 1
        return False

    await ig.browse_feed(20.0, like_gate=_denied_gate)  # non deve sollevare

    assert page.mouse.click_calls == 0, "tetto negato: zero like, sempre"
    assert gate_calls["n"] >= 1, "il gate va interpellato almeno una volta"


@pytest.mark.asyncio
async def test_browse_feed_no_gate_injected_never_likes_fail_safe(monkeypatch):
    """Senza like_gate iniettata (None) il ramo like resta SEMPRE disattivato
    -- fail SAFE: un chiamante che dimentica di collegare il tetto non mette
    mai like invece di metterne senza limite."""
    page = _FakePage(like_count=1, home_nav_count=0)
    ig = _make_ig(page)
    _patch_deterministic_loop(ig, monkeypatch, max_likes_choice=2)

    await ig.browse_feed(20.0)  # like_gate default None

    assert page.mouse.click_calls == 0


@pytest.mark.asyncio
async def test_browse_feed_granted_respects_session_local_max_likes(monkeypatch):
    """Il gate DB e il max_likes locale sono DUE limiti indipendenti: anche
    con un gate che concede SEMPRE, il click si ferma al tetto di sessione
    (qui 2) -- il like parte solo se ENTRAMBI lo consentono."""
    page = _FakePage(like_count=1, home_nav_count=0)
    ig = _make_ig(page)
    _patch_deterministic_loop(ig, monkeypatch, max_likes_choice=2)

    async def _always_granted() -> bool:
        return True

    await ig.browse_feed(20.0, like_gate=_always_granted)

    assert page.mouse.click_calls == 2, "il tetto DI SESSIONE resta 2, gate permissivo o no"


@pytest.mark.asyncio
async def test_browse_feed_never_lands_on_target_profile_before_liking(monkeypatch):
    """Invariante 5: se browse_feed viene invocata mentre la pagina e' ferma
    su un profilo (es. appena visitato per lo scraping), la funzione
    rinaviga SEMPRE verso l'home feed PRIMA di qualunque interazione --
    quindi un like non puo' mai scattare sul profilo target, solo su un post
    dell'home feed."""
    page = _FakePage(
        start_url="https://www.instagram.com/il_target_scrapato/",
        like_count=1, home_nav_count=0,  # home_nav assente -> forza il goto di fallback
    )
    ig = _make_ig(page)
    _patch_deterministic_loop(ig, monkeypatch, max_likes_choice=0)  # niente like: qui conta solo la nav

    await ig.browse_feed(5.0, like_gate=None)

    assert any(u.rstrip("/") == ig.BASE_URL for u in page.goto_calls), (
        "partendo da un profilo, browse_feed deve navigare verso l'home feed"
    )
    assert page.url.rstrip("/") == ig.BASE_URL, "la pagina finisce sull'home feed, mai sul profilo"


@pytest.mark.asyncio
async def test_browse_feed_navigation_failure_disables_likes_this_session(monkeypatch):
    """RILIEVO review: il ramo 'nessun like puo' scattare su un profilo
    target' NON era vero nel caso in cui il tentativo di navigazione verso
    l'home fallisce (home_nav assente + goto che solleva): il try/except
    esterno ingoia l'errore e prosegue 'sulla pagina corrente', che nel caso
    peggiore e' il profilo di un target appena scrapato. Qui la pagina parte
    proprio su un profilo target e il goto di ripiego SOLLEVA -- prova che:
    (a) browse_feed non propaga comunque l'eccezione (sessione difensiva);
    (b) i like restano SPENTI per l'intera sessione: zero click, il gate
    di prenotazione non viene nemmeno interpellato (nessuna scrittura DB
    inutile per un like che non sarebbe comunque scattato)."""
    page = _FakePage(
        start_url="https://www.instagram.com/il_target_scrapato/",
        like_count=1, home_nav_count=0,  # home_nav assente -> forza il goto di fallback
        raise_on_goto=True,
    )
    ig = _make_ig(page)
    # max_likes>0 e like_gate sempre permissiva: se il fix non ci fosse,
    # questo scenario produrrebbe click reali sul profilo target.
    _patch_deterministic_loop(ig, monkeypatch, max_likes_choice=2)

    gate_calls = {"n": 0}

    async def _always_granted() -> bool:
        gate_calls["n"] += 1
        return True

    await ig.browse_feed(20.0, like_gate=_always_granted)  # non deve sollevare

    assert page.mouse.click_calls == 0, "navigazione fallita: zero like per tutta la sessione"
    assert gate_calls["n"] == 0, "il gate non va nemmeno interpellato se l'home non e' confermata"
