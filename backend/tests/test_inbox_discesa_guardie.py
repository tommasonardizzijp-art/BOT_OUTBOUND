"""Guardie e osservabilita' della DISCESA nell'inbox (fase Lista API).

Contesto (22/08): la campagna "DM Claudio x AV" si e' fermata a 2999 contatti
dichiarando `ready` — cioe' "finito" — col cursore ancora fermo al 25 febbraio.
Non sapremo mai perche': non c'era un solo log utile. Il target di 10.000 era
un'impostazione dell'utente, non una misura dell'inbox, quindi quel numero NON
prova che ci fosse un tetto: quell'inbox poteva finire davvero li'. Resta che
una discesa puo' morire in silenzio dicendo che e' andata bene, e che oggi non
avremmo modo di distinguere i due casi.

Il principio comune di questi test: un motore che si ferma deve saper dire SE ha
finito davvero o se qualcuno gli ha detto di smettere. Sono due cose diverse e
prima erano indistinguibili.
"""
import asyncio
import os
import sys
import tempfile
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.models.account          # noqa: F401 — register in metadata
import app.models.campaign_account  # noqa: F401
import app.models.message          # noqa: F401
import app.models.activity_log     # noqa: F401
import app.models.global_contact   # noqa: F401

from app.database import Base
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower
from app.services import scrape_inbox
from app.services.inbox_source import InboxPage

PIENA = 20   # thread per pagina richiesti a IG (limit=20)


class _ScriptedSource:
    """Restituisce le pagine preparate, poi una pagina finale esaurita."""

    def __init__(self, pages):
        self._pages = list(pages)
        self.chiamate = 0

    async def next_page(self):
        self.chiamate += 1
        if self._pages:
            return self._pages.pop(0)
        return InboxPage(participants=[], cursor=None, exhausted=True)


def _pagina(*, n_part=0, threads=PIENA, cursor="C", bottom=False, has_older=True,
            base=0, con_utenti=None):
    """Una pagina di inbox. `base` sposta i pk per non collidere fra pagine."""
    return InboxPage(
        participants=[(base + i + 1, "u{}".format(base + i + 1)) for i in range(n_part)],
        cursor=cursor,
        exhausted=False,
        bottom_confirmed=bottom,
        threads_letti=threads,
        has_older=has_older,
        # Per default i thread portano i loro utenti: e' il caso normale, gruppi
        # inclusi. `con_utenti=0` simula il payload degradato (nessun dato utente).
        threads_con_utenti=threads if con_utenti is None else con_utenti,
    )


def _setup(monkeypatch, pages, *, bottom_reached=False, deep_pages=0):
    fd, path = tempfile.mkstemp(suffix=".db", prefix="inbox_guardie_")
    os.close(fd)
    engine = create_async_engine("sqlite+aiosqlite:///{}".format(path),
                                 connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    campaign_id = str(uuid.uuid4())
    src = _ScriptedSource(pages)

    async def _seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as db:
            db.add(Campaign(
                id=campaign_id, name="guardie discesa", source_type="scrape",
                scrape_mode="dm_threads", inbox_engine="api",
                status=CampaignStatus.listing, messaging_enabled=False,
                inbox_bottom_reached=bottom_reached,
                inbox_deep_pages=deep_pages,
                scrape_session_size=100_000,
                scrape_break_minutes_min=30, scrape_break_minutes_max=45,
            ))
            await db.commit()

    asyncio.run(_seed())

    async def _fake_build(db, campaign):
        async def _noop():
            return None
        return src, 999_999, None, _noop

    monkeypatch.setattr(scrape_inbox, "build_inbox_source", _fake_build)

    # Il pacing vero fra pagine e' 10-60s: in un test da centinaia di pagine
    # sarebbero ore. Si azzera l'attesa, non la logica che la decide.
    async def _senza_attesa(*a, **k):
        return None

    monkeypatch.setattr(scrape_inbox, "_inbox_page_delay", _senza_attesa)

    def cleanup():
        asyncio.run(engine.dispose())
        try:
            os.unlink(path)
        except OSError:
            pass

    return factory, campaign_id, src, cleanup


def _run(factory, campaign_id, timeout=60):
    async def _go():
        async with factory() as db:
            campaign = await db.get(Campaign, campaign_id)
            return await scrape_inbox.run_inbox_list(campaign_id, db, campaign)
    return asyncio.run(asyncio.wait_for(_go(), timeout=timeout))


def _stato(factory, campaign_id):
    async def _go():
        async with factory() as db:
            c = await db.get(Campaign, campaign_id)
            n = await db.scalar(select(func.count(Follower.id))
                                .where(Follower.campaign_id == campaign_id))
            return c, n
    return asyncio.run(_go())


def _spia_log(monkeypatch):
    """Cattura i messaggi che il motore logga, divisi per livello."""
    reale = scrape_inbox.logger
    catturati = {"info": [], "warning": [], "error": [], "debug": []}

    class _Proxy:
        def info(self, m, *a, **k):
            catturati["info"].append(str(m))

        def warning(self, m, *a, **k):
            catturati["warning"].append(str(m))

        def error(self, m, *a, **k):
            catturati["error"].append(str(m))

        def debug(self, m, *a, **k):
            catturati["debug"].append(str(m))

        def exception(self, m, *a, **k):
            catturati["error"].append(str(m))

        def __getattr__(self, n):
            return getattr(reale, n)

    monkeypatch.setattr(scrape_inbox, "logger", _Proxy())
    return catturati


# ─────────────────────── 1. il tetto delle pagine non c'e' piu' ────────────
def test_la_discesa_non_si_ferma_oltre_le_vecchie_500_pagine(monkeypatch):
    """Tommaso vende 20.000 contatti: il vecchio tetto a 500 pagine (= 10.000
    thread) gli avrebbe chiuso il lavoro a meta' DICHIARANDO di aver finito.

    Si parte da una campagna gia' a 600 pagine di discesa — cioe' ben oltre il
    tetto rimosso — e si verifica che il giro prosegua e raccolga: prima si
    sarebbe fermato alla prima pagina con un warning."""
    pagine = [_pagina(n_part=2, base=0, cursor="C0"),
              _pagina(n_part=2, base=100, cursor="C1"),
              _pagina(n_part=2, base=200, cursor="C2")]
    factory, cid, src, cleanup = _setup(monkeypatch, pagine, deep_pages=600)
    try:
        _run(factory, cid)
        c, n = _stato(factory, cid)
        assert n == 6, "oltre le 500 pagine la discesa deve proseguire: trovati {}".format(n)
        assert c.inbox_deep_pages >= 603
        assert c.inbox_bottom_reached is False
    finally:
        cleanup()


def test_il_setting_del_tetto_non_esiste_piu():
    """Il tetto non deve sopravvivere come costante inerte: si rimuove."""
    from app.config import settings
    assert not hasattr(settings, "inbox_deep_max_pages")


# ─────────────────────── 2. il fondo falso su pagina piena ────────────────
def test_fondo_dichiarato_su_pagina_PIENA_non_alza_linterruttore(monkeypatch):
    """LA guardia piu' importante. Una lista che finisce davvero ha l'ultima
    pagina PARZIALE: se ci sono 9.412 conversazioni, l'ultima ne porta 12, non 20.
    Pagina piena + "non c'e' altro sotto" e' una contraddizione — ed e' anche il
    modo piu' economico che avrebbe IG per mettere un tetto di profondita' senza
    restituire un errore. Crederci alza `inbox_bottom_reached`, che e' PERMANENTE:
    si perde il resto dell'inbox per sempre, e l'unico reset esistente cancella
    tutti i Message della campagna."""
    pagine = [_pagina(n_part=3, base=0, cursor="C0"),
              _pagina(n_part=3, base=100, cursor="C1", bottom=True, threads=PIENA,
                      has_older=False)]
    factory, cid, src, cleanup = _setup(monkeypatch, pagine)
    spia = _spia_log(monkeypatch)
    try:
        _run(factory, cid)
        c, n = _stato(factory, cid)
        assert c.inbox_bottom_reached is False, \
            "fondo dichiarato su pagina piena: NON si alza l'interruttore permanente"
        assert c.inbox_deep_cursor is not None, "la frontiera non va persa"
        assert any("piena" in m.lower() for m in spia["warning"]), \
            "serve un warning esplicito, trovati: {}".format(spia["warning"])
    finally:
        cleanup()


def test_fondo_dichiarato_su_pagina_PARZIALE_alza_linterruttore(monkeypatch):
    """Il caso legittimo deve continuare a funzionare: ultima pagina non piena +
    has_older=False = l'inbox e' finito davvero."""
    pagine = [_pagina(n_part=3, base=0, cursor="C0"),
              _pagina(n_part=2, base=100, cursor="C1", bottom=True, threads=7,
                      has_older=False)]
    factory, cid, src, cleanup = _setup(monkeypatch, pagine)
    try:
        _run(factory, cid)
        c, n = _stato(factory, cid)
        assert c.inbox_bottom_reached is True
        assert c.inbox_deep_cursor is None
    finally:
        cleanup()


# ─────────────────────── 3. pagine senza partecipanti estraibili ──────────
def test_pagine_piene_di_thread_ma_senza_partecipanti_fermano_la_discesa(monkeypatch):
    """Se IG servisse thread senza i dati utente, `extract_thread_participant` li
    scarta tutti e la pagina risulta "nessun contatto nuovo" — IDENTICA a una
    pagina di gente gia' in lista. Il motore direbbe "inbox gia' raccolto" e
    chiuderebbe con un messaggio di successo mentre gli stanno servendo pagine
    vuote. Va distinto e va fermato con una ragione propria."""
    pagine = [_pagina(n_part=2, base=0, cursor="C0")]
    pagine += [_pagina(n_part=0, threads=PIENA, con_utenti=0,
                       cursor="CV{}".format(i))
               for i in range(6)]
    factory, cid, src, cleanup = _setup(monkeypatch, pagine)
    spia = _spia_log(monkeypatch)
    try:
        _run(factory, cid)
        c, n = _stato(factory, cid)
        assert c.inbox_bottom_reached is False, "non e' il fondo: e' una resa"
        assert any("username" in m.lower() or "partecipant" in m.lower()
                   for m in spia["warning"]), \
            "serve un warning dedicato, trovati: {}".format(spia["warning"])
        assert src.chiamate <= 5, \
            "doveva fermarsi presto, ha chiesto {} pagine".format(src.chiamate)
    finally:
        cleanup()


def test_poche_pagine_di_soli_gruppi_NON_fermano_la_discesa(monkeypatch):
    """Falso positivo da evitare, trovato da un test gia' esistente: un tratto di
    chat di GRUPPO da' zero partecipanti 1-a-1 ed e' del tutto legittimo. La
    differenza col payload degradato non e' il conteggio (identico) ma il fatto
    che nei gruppi gli utenti CI SONO, sono solo piu' di uno."""
    pagine = [_pagina(n_part=2, base=0, cursor="C0"),
              _pagina(n_part=0, threads=PIENA, cursor="C1"),
              _pagina(n_part=2, base=100, cursor="C3")]
    factory, cid, src, cleanup = _setup(monkeypatch, pagine)
    try:
        _run(factory, cid)
        c, n = _stato(factory, cid)
        assert n == 4, "i contatti dopo il tratto di gruppi vanno presi: trovati {}".format(n)
    finally:
        cleanup()


# ─────────────────────── 4. pagina corta: avvisa, non ferma ───────────────
def test_pagina_corta_avvisa_ma_NON_ferma_la_discesa(monkeypatch):
    """Una pagina da 5 thread quando ne chiediamo 20, con has_older ancora vero,
    e' il modo tipico in cui un servizio frena senza dirlo. Decisione di Tommaso
    (22/08): per ora si LOGGA soltanto — potrebbe avere altre cause e fermare a
    caso costerebbe piu' di quanto salva."""
    pagine = [_pagina(n_part=2, base=0, cursor="C0"),
              _pagina(n_part=1, base=100, threads=5, cursor="C1"),
              _pagina(n_part=2, base=200, cursor="C2")]
    factory, cid, src, cleanup = _setup(monkeypatch, pagine)
    spia = _spia_log(monkeypatch)
    try:
        _run(factory, cid)
        c, n = _stato(factory, cid)
        assert n == 5, "la discesa deve proseguire: trovati {} contatti".format(n)
        assert any("corta" in m.lower() for m in spia["warning"]), \
            "serve l'avviso di pagina corta, trovati: {}".format(spia["warning"])
    finally:
        cleanup()


# ─────────────────────── 5. il log di pagina ──────────────────────────────
def test_il_log_di_pagina_riporta_has_older_cursore_e_latenza(monkeypatch):
    """Senza questi tre, "0 nuovi" nel log puo' voler dire cinque cose diverse e
    non si distinguono. Col cursore si legge anche a che DATA si e' arrivati:
    e' il dato che dice quanto e' grande davvero l'inbox dopo mezz'ora invece
    che dopo due settimane."""
    cursore = ('{"cursor_thread_v2_id":1,"cursor_timestamp_seconds":1772054057,'
               '"cursor_relevancy_score":0}')
    pagine = [_pagina(n_part=2, base=0, cursor=cursore)]
    factory, cid, src, cleanup = _setup(monkeypatch, pagine)
    spia = _spia_log(monkeypatch)
    try:
        _run(factory, cid)
        righe = " | ".join(spia["info"])
        assert "has_older" in righe, "manca has_older nel log: {}".format(righe)
        assert "2026-02-25" in righe, \
            "la data del cursore va decodificata e mostrata: {}".format(righe)
        assert "ms" in righe, "manca la latenza: {}".format(righe)
    finally:
        cleanup()


def test_un_cursore_illeggibile_non_rompe_il_log(monkeypatch):
    """Il parsing del cursore e' un ORNAMENTO: se IG cambia formato, il log
    perde una data, non il giro. Fail-open."""
    pagine = [_pagina(n_part=2, base=0, cursor="non-json-affatto")]
    factory, cid, src, cleanup = _setup(monkeypatch, pagine)
    try:
        _run(factory, cid)
        c, n = _stato(factory, cid)
        assert n == 2, "il giro prosegue anche con un cursore non decodificabile"
    finally:
        cleanup()


# ─────────── 6. il secondo giro: il punto cieco trovato in review ──────────
def _riporta_in_listing(factory, campaign_id):
    """Rimette la campagna in listing come farebbe un rilancio dalla UI."""
    async def _go(db=None):
        async with factory() as db:
            c = await db.get(Campaign, campaign_id)
            c.status = CampaignStatus.listing
            await db.commit()
    asyncio.run(_go())


def test_un_inbox_multiplo_esatto_di_20_non_resta_bloccato_per_sempre(monkeypatch):
    """Il difetto trovato in review, e il piu' pericoloso di tutti.

    Un inbox con esattamente N x 20 conversazioni ha l'ultima pagina PIENA per
    davvero. La guardia del fondo falso la rifiutava. Ma se Instagram non manda un
    cursore su quell'ultima pagina la frontiera resta ferma, e OGNI rilancio
    ritrova la stessa pagina e la rifiuta di nuovo: per sempre, con un alert a
    ogni giro, e senza mai passare in modalita' cima — quindi smettendo anche di
    raccogliere i DM nuovi.

    Il criterio che scioglie il nodo e' il cursore della pagina stessa: senza
    cursore la frontiera non puo' avanzare, quindi rifiutare vorrebbe dire
    rifiutare per sempre. Si accetta subito. Il secondo giro qui serve a provare
    che il loop non c'e': la campagna e' passata in cima e non ripete niente."""
    ultima = _pagina(n_part=3, base=100, cursor=None, bottom=True,
                     threads=PIENA, has_older=False)
    factory, cid, src, cleanup = _setup(
        monkeypatch, [_pagina(n_part=3, base=0, cursor="C0"), ultima])
    try:
        _run(factory, cid)
        c, _ = _stato(factory, cid)
        assert c.inbox_bottom_reached is True,             "senza cursore il rifiuto sarebbe eterno: il fondo si accetta subito"
        assert c.inbox_deep_cursor is None

        # Secondo giro: ora e' in modalita' cima, non ridiscende e non ripete
        # l'alert. Prima di questa correzione qui ricominciava il loop.
        src._pages = [_pagina(n_part=0, threads=0, cursor=None)]
        _riporta_in_listing(factory, cid)
        _run(factory, cid)
        c, _ = _stato(factory, cid)
        assert c.inbox_bottom_reached is True, "l'interruttore resta alzato"
    finally:
        cleanup()


def test_la_guardia_del_fondo_non_scatta_in_modalita_cima(monkeypatch):
    """In cima non si sta scendendo: un inbox piccolo e multiplo di 20 arriva in
    fondo a ogni passata, e sparare un alert 'sospetto un limite di profondita''
    ogni volta e' rumore che addestra a ignorare gli alert."""
    pagine = [_pagina(n_part=2, base=0, cursor="C0", bottom=True,
                      threads=PIENA, has_older=False)]
    factory, cid, src, cleanup = _setup(monkeypatch, pagine, bottom_reached=True)
    spia = _spia_log(monkeypatch)
    try:
        _run(factory, cid)
        assert not any("pagina PIENA" in m for m in spia["warning"]), \
            "in modalita' cima la guardia della discesa non deve parlare"
    finally:
        cleanup()


# ─────────── 7. buchi di copertura segnalati in review ────────────────────
def test_una_pagina_NON_misurata_non_fa_scattare_la_guardia(monkeypatch):
    """`threads_con_utenti=None` significa 'non misurato' — pagine costruite da
    sorgenti che non hanno un payload da ispezionare. Su un dato che non abbiamo
    la guardia deve tacere: sconosciuto non e' un'accusa."""
    pagine = [_pagina(n_part=2, base=0, cursor="C0")]
    pagine += [InboxPage(participants=[], cursor="CN{}".format(i), exhausted=False,
                         threads_letti=PIENA, has_older=True,
                         threads_con_utenti=None) for i in range(6)]
    pagine += [_pagina(n_part=2, base=500, cursor="C9")]
    factory, cid, src, cleanup = _setup(monkeypatch, pagine)
    spia = _spia_log(monkeypatch)
    try:
        _run(factory, cid)
        c, n = _stato(factory, cid)
        assert n == 4, "il giro prosegue su pagine non misurate: trovati {}".format(n)
        assert not any("utenti leggibili" in m for m in spia["warning"])
    finally:
        cleanup()


def test_la_latenza_finisce_davvero_nel_log(monkeypatch):
    """L'assert precedente su 'ms' passava anche cancellando la misura, perche'
    nei test la latenza e' sempre None e il log stampa il fallback '?ms'."""
    import re
    pagina = _pagina(n_part=2, base=0, cursor="C0")
    pagina.latenza_ms = 437
    factory, cid, src, cleanup = _setup(monkeypatch, [pagina])
    spia = _spia_log(monkeypatch)
    try:
        _run(factory, cid)
        righe = " | ".join(spia["info"])
        assert re.search(r"latenza=\d+ms", righe), \
            "la latenza misurata deve comparire come numero: {}".format(righe)
    finally:
        cleanup()


def test_la_discesa_si_ferma_se_non_produce_piu_niente(monkeypatch):
    """Rete di sicurezza al posto del vecchio tetto a pagine: pagine piene,
    cursori sempre nuovi, utenti veri ma tutti gia' in lista. Nessuna delle altre
    guardie lo vede e il giro scenderebbe all'infinito bruciando chiamate.

    Gira con `inbox_session_pages` VERO, cioe' attraverso piu' giri. La prima
    versione di questo test alzava quel valore a 10.000 ed era verde su un mondo
    che non esiste: il contatore era locale, la funzione esce ogni 15 pagine, e la
    rete non poteva scattare mai. Il contatore ora e' persistito in DB (038) ed e'
    questo test a doverlo dimostrare — quindi il valore di sessione non si tocca.
    """
    from app.config import settings
    monkeypatch.setattr(settings, "inbox_discesa_senza_lavoro_stop", 20)
    # stessi partecipanti a ogni pagina: dopo la prima non producono piu' nulla
    pagine = [_pagina(n_part=1, base=0, cursor="D{}".format(i)) for i in range(200)]
    factory, cid, src, cleanup = _setup(monkeypatch, pagine)
    spia = _spia_log(monkeypatch)
    try:
        # Piu' giri, come in produzione: ognuno legge 15 pagine e va in pausa.
        for _ in range(10):
            _run(factory, cid)
            c, _ = _stato(factory, cid)
            if c.status != CampaignStatus.listing_break:
                break
            _riporta_in_listing(factory, cid)

        c, n = _stato(factory, cid)
        assert c.inbox_bottom_reached is False, "non e' il fondo, e' una resa"
        assert src.chiamate < 100,             ("la rete doveva fermare la discesa dopo ~21 pagine, "
             "ne ha chieste {}".format(src.chiamate))
        assert any("senza un solo" in m for m in spia["warning"]),             "serve un warning dedicato: {}".format(spia["warning"])
    finally:
        cleanup()


def test_il_contatore_del_lavoro_a_vuoto_sopravvive_alla_pausa_di_sessione(monkeypatch):
    """Il cuore del difetto trovato in review: un contatore LOCALE si azzera a
    ogni uscita dalla funzione, e la funzione esce ogni 15 pagine. Qualunque
    soglia sopra 15 sarebbe inerte. Deve stare in DB."""
    from app.config import settings
    monkeypatch.setattr(settings, "inbox_discesa_senza_lavoro_stop", 0)  # rete spenta
    pagine = [_pagina(n_part=1, base=0, cursor="E{}".format(i)) for i in range(40)]
    factory, cid, src, cleanup = _setup(monkeypatch, pagine)
    try:
        _run(factory, cid)          # primo giro: 15 pagine, poi pausa
        c, _ = _stato(factory, cid)
        primo = c.inbox_deep_senza_lavoro
        assert primo >= 14, "dopo un giro il contatore deve gia' essere alto: {}".format(primo)

        _riporta_in_listing(factory, cid)
        _run(factory, cid)          # secondo giro
        c, _ = _stato(factory, cid)
        assert c.inbox_deep_senza_lavoro > primo,             ("il contatore deve PROSEGUIRE fra un giro e l'altro, non ripartire: "
             "{} -> {}".format(primo, c.inbox_deep_senza_lavoro))
    finally:
        cleanup()


def test_un_fondo_finto_dopo_la_pausa_di_sessione_viene_comunque_rifiutato(monkeypatch):
    """Lo scenario che il criterio precedente lasciava passare in SILENZIO.

    La prima pagina di un giro ripreso non e' "un fondo gia' rifiutato": e' una
    pagina mai vista. Col vecchio criterio la guardia era spenta proprio li' —
    una pagina ogni quindici — e un fondo falso alzava l'interruttore permanente
    chiudendo con il messaggio normale di successo. Col criterio sul cursore la
    guardia resta accesa: la pagina porta un cursore, quindi rifiutarla costa un
    giro, non l'eternita'."""
    pagine = [_pagina(n_part=2, base=0, cursor="C0"),
              _pagina(n_part=2, base=100, cursor="C1", bottom=True,
                      threads=PIENA, has_older=False)]
    factory, cid, src, cleanup = _setup(monkeypatch, pagine)
    # la pagina del fondo cade come PRIMA pagina di un giro ripreso
    async def _riprendi_da_C0(db=None):
        async with factory() as db:
            c = await db.get(Campaign, cid)
            c.inbox_deep_cursor = "C0"
            await db.commit()
    asyncio.run(_riprendi_da_C0())
    src._pages = [pagine[1]]
    spia = _spia_log(monkeypatch)
    try:
        _run(factory, cid)
        c, _ = _stato(factory, cid)
        assert c.inbox_bottom_reached is False,             "un fondo mai visto prima non si accetta solo perche' apre un giro"
        assert any("piena" in m.lower() for m in spia["warning"])
    finally:
        cleanup()


def test_un_fondo_su_pagina_piena_SENZA_cursore_si_accetta(monkeypatch):
    """L'altra faccia: senza cursore la frontiera non avanza, quindi ogni rilancio
    ritroverebbe questa identica pagina. Rifiutarla significherebbe rifiutarla per
    sempre — e la campagna non passerebbe mai in modalita' cima, smettendo anche
    di raccogliere i DM nuovi."""
    pagine = [_pagina(n_part=2, base=0, cursor="C0"),
              _pagina(n_part=2, base=100, cursor=None, bottom=True,
                      threads=PIENA, has_older=False)]
    factory, cid, src, cleanup = _setup(monkeypatch, pagine)
    try:
        _run(factory, cid)
        c, _ = _stato(factory, cid)
        assert c.inbox_bottom_reached is True,             "senza cursore rifiutare vorrebbe dire rifiutare per sempre"
    finally:
        cleanup()


def test_la_guardia_dei_partecipanti_vale_ANCHE_in_modalita_cima(monkeypatch):
    """Chiuso un rilievo disattivando la protezione invece di correggere il
    messaggio: il `not modo_cima` toglieva l'avviso sbagliato ("Discesa
    interrotta" in un giro che non scende) togliendo anche la guardia.

    In cima serve piu' che altrove: dopo il fondo la cima e' il regime ordinario
    della campagna, e li' un payload degradato produceva il messaggio "inbox gia'
    tutto raccolto" — un successo dichiarato su un giro che non ha raccolto
    niente, cioe' esattamente cio' che questa guardia esiste per impedire."""
    pagine = [InboxPage(participants=[], cursor="CC{}".format(i), exhausted=False,
                        threads_letti=PIENA, has_older=True,
                        threads_con_utenti=0) for i in range(6)]
    factory, cid, src, cleanup = _setup(monkeypatch, pagine, bottom_reached=True)
    spia = _spia_log(monkeypatch)
    try:
        _run(factory, cid)
        assert any("utenti leggibili" in m for m in spia["warning"]), \
            "in cima la guardia deve parlare: {}".format(spia["warning"])
        assert src.chiamate <= 5, \
            "doveva fermarsi presto, ha chiesto {} pagine".format(src.chiamate)
    finally:
        cleanup()
