"""Batteria ADVERSARIALE di chiusura Fase 4 (standard sviluppo-modulo) per il
modulo M4 (reply-watcher + opt-out WhatsApp). Obiettivo dichiarato: ROMPERE il
sistema, non dimostrare che "funziona". PASS INVERTITO: un test passa se il
sistema SI DIFENDE (errore chiaro, nessuna scrittura sporca, invariante
intatta); un'eccezione grezza, una scrittura parziale o un'invariante violata
sono FAIL anche se "sembrava funzionare".

Copre: concorrenza vera fra i tre consumatori del lucchetto profilo (mai
provata con vera concorrenza prima d'ora), stringhe ostili nella preview/title
della chat, macchina a stati del numero sotto stress, l'irrilevanza di
WA_SEND_ENABLED per il watcher, e le invarianti di dominio via query SQL
dirette (non solo asserzioni ORM in memoria).
"""
import asyncio
import contextlib
import uuid

import fakeredis.aioredis
import pytest
from sqlalchemy import select, text, update

from app.browser.whatsapp_page import ChatRow
from app.config import settings
from app.database import AsyncSessionLocal
from app.models.wa import (WaCampaignStatus, WaContactStatus, WaInboundEvent,
                           WaMatchedBy, WaNumber, WaNumberStatus)
from app.services import bot_state_service, wa_profile_lock, wa_reply_watcher
from app.workers import cron_worker, wa_worker
from tests.factories_wa import (make_campaign, make_campaign_contact,
                                make_contact, make_number, make_tenant)


def _row(title, *, title_is_number=False, preview="ciao", unread=1):
    return ChatRow(position=0, title=title, title_is_number=title_is_number,
                   unread_count=unread, preview=preview, last_is_outbound=False,
                   outgoing_state=None, muted=False)


async def _mai_halted() -> bool:
    return False


def _lock_profilo_libero(monkeypatch):
    """Lucchetto no-op: stesso pattern di test_wa_reply_watcher.py, usato dai
    test che non riguardano la concorrenza del lock."""
    @contextlib.asynccontextmanager
    async def _libero(number_id, *, ttl_min=None):
        yield "token-di-test"
    monkeypatch.setattr(wa_reply_watcher.wa_profile_lock, "held", _libero)


@pytest.fixture
def fake_redis(monkeypatch):
    """Lock REALE (non no-op) contro fakeredis: qui serve la vera mutua
    esclusione fra i tre consumatori, non un doppio che la scavalca -- stesso
    pattern di test_wa_profile_lock.py."""
    client = fakeredis.aioredis.FakeRedis()

    async def _fake_pool():
        return client

    monkeypatch.setattr(wa_profile_lock.arq, "create_pool", lambda *_a, **_k: _fake_pool())
    return client


class _ContatoreAperture:
    """Conta le aperture 'browser' concorrenti. Se max_visto supera 1, due
    consumatori hanno avuto il profilo aperto nello stesso istante -- la
    garanzia esatta che wa_profile_lock deve impedire."""
    def __init__(self):
        self.correnti = 0
        self.max_visto = 0
        self.aperture_totali = 0

    async def apri(self):
        self.correnti += 1
        self.aperture_totali += 1
        self.max_visto = max(self.max_visto, self.correnti)
        await asyncio.sleep(0.1)

    async def chiudi(self):
        self.correnti -= 1


def _fake_browser_ctx(contatore):
    """Sostituto di _open_wa_browser: incrementa il contatore su __aenter__,
    lo decrementa su __aexit__. Stesso schema (__call__ ritorna self) dei
    finti browser-context in test_wa_reply_watcher.py."""
    class _Page:
        async def goto(self, *a, **k):
            pass

    class _Context:
        async def new_page(self):
            return _Page()

    class _Ctx:
        def __call__(self, number_id, headless=True, proxy_url=None):
            return self

        async def __aenter__(self):
            await contatore.apri()
            return _Context()

        async def __aexit__(self, *a):
            await contatore.chiudi()
            return False

    return _Ctx()


class _PomVuoto:
    """WhatsAppWebPage finto: sessione pronta, sidebar vuota -- basta per
    superare scan_number senza toccare selettori veri."""
    def __init__(self, page):
        pass

    async def session_state(self):
        return "logged_in"

    async def scan_chat_list(self):
        return []


# ---------------------------------------------------------------------------
# 1) Concorrenza vera fra i 3 consumatori del profilo
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concorrenza_reale_tre_consumatori_stesso_numero(db_session, monkeypatch, fake_redis):
    """La garanzia CENTRALE del modulo (mai provata con concorrenza vera in
    questo branch -- lo segnala anche la review finale come mancante):
    wa_worker.esegui_mini_sessione, cron_worker.wa_session_healthcheck e
    wa_reply_watcher.scan_number sullo STESSO number_id, in asyncio.gather
    VERO. Al piu' UNO deve entrare nel blocco 'browser aperto'; gli altri due
    devono ricevere un esito coerente (profilo occupato) senza eccezioni non
    gestite."""
    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    await db_session.commit()

    contatore = _ContatoreAperture()

    monkeypatch.setattr(settings, "wa_send_enabled", True)
    monkeypatch.setattr(bot_state_service, "is_wa_halted", _mai_halted)

    monkeypatch.setattr(wa_reply_watcher, "_open_wa_browser", _fake_browser_ctx(contatore))
    monkeypatch.setattr(wa_reply_watcher, "WhatsAppWebPage", _PomVuoto)
    monkeypatch.setattr(wa_worker, "_open_wa_browser", _fake_browser_ctx(contatore))

    async def _fake_check_session(number_id):
        # check_session (M1) e' l'operazione 'cara' del consumatore
        # health-check: qui la simuliamo con lo stesso contatore condiviso,
        # cosi' l'invariante 'al piu' uno aperto alla volta' vale sui TRE
        # consumatori reali, non solo su due. Ma il DB di test e' CONDIVISO
        # fra i file della suite (altri test lasciano numeri attivi dietro
        # di se'): wa_session_healthcheck({}) itera TUTTI i numeri attivi,
        # non solo quello sotto test. Contiamo solo le aperture per
        # `numero.id` -- gli altri numeri hanno una chiave di lock diversa,
        # la loro concorrenza reciproca non e' l'invariante che proviamo qui.
        if number_id != numero.id:
            return WaNumberStatus.active
        await contatore.apri()
        try:
            return WaNumberStatus.active
        finally:
            await contatore.chiudi()
    monkeypatch.setattr(cron_worker, "check_session", _fake_check_session)

    risultati = await asyncio.gather(
        wa_worker.esegui_mini_sessione(numero.id),
        cron_worker.wa_session_healthcheck({}),
        wa_reply_watcher.scan_number(numero.id),
        return_exceptions=True,
    )

    for r in risultati:
        assert not isinstance(r, Exception), f"eccezione non gestita: {r!r}"

    assert contatore.max_visto <= 1, (
        f"due consumatori hanno avuto il browser 'aperto' insieme (max={contatore.max_visto})")
    assert contatore.aperture_totali >= 1, "nessuno dei tre e' mai riuscito ad acquisire il lock"

    invio, health, scan = risultati
    assert invio["motivo"] in ("niente_da_fare", "profilo_occupato")
    assert health["saltati_invio_attivo"] in (0, 1)
    assert scan["motivo"] in (None, "profilo_occupato")


@pytest.mark.asyncio
async def test_due_scan_number_concorrenti_stesso_numero_lock_reale(db_session, monkeypatch, fake_redis):
    """Due wa_reply_watcher.scan_number VERI in asyncio.gather sullo stesso
    numero, lock reale via fakeredis: uno vince, l'altro riceve
    motivo='profilo_occupato' pulito, zero aperture doppie del mock-browser."""
    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    await db_session.commit()

    contatore = _ContatoreAperture()
    monkeypatch.setattr(wa_reply_watcher, "_open_wa_browser", _fake_browser_ctx(contatore))
    monkeypatch.setattr(wa_reply_watcher, "WhatsAppWebPage", _PomVuoto)
    monkeypatch.setattr(bot_state_service, "is_wa_halted", _mai_halted)

    r1, r2 = await asyncio.gather(
        wa_reply_watcher.scan_number(numero.id),
        wa_reply_watcher.scan_number(numero.id),
    )

    motivi = {r1["motivo"], r2["motivo"]}
    assert motivi == {None, "profilo_occupato"}, f"esiti inattesi: {r1['motivo']!r}, {r2['motivo']!r}"
    assert contatore.max_visto <= 1
    assert contatore.aperture_totali == 1, "il perdente non deve mai aprire il browser"


# ---------------------------------------------------------------------------
# 2) Stringhe ostili nella preview/title della chat
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preview_sql_injection_letterale_salvata_come_stringa_innocua(db_session):
    """SQLAlchemy parametrizza sempre le query: una preview con sintassi SQL
    ostile deve finire come DATO, non come comando. La tabella non sparisce e
    il testo torna identico byte per byte."""
    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    contatto.chat_title = "Marco"
    await db_session.commit()

    ostile = "'; DROP TABLE wa_inbound_events; --"
    esito = await wa_reply_watcher.process_chat_row(
        db_session, tenant_id=tenant.id, wa_number_id=numero.id,
        row=_row("Marco", preview=ostile))
    assert esito["esito"] == "ignorato"

    eventi = (await db_session.execute(
        select(WaInboundEvent).where(WaInboundEvent.contact_id == contatto.id)
    )).scalars().all()
    assert len(eventi) == 1
    assert eventi[0].preview_text == ostile

    # La tabella esiste ancora ed e' interrogabile: se l'iniezione avesse
    # avuto un qualunque effetto, questa query stessa fallirebbe.
    conteggio = await db_session.scalar(select(WaInboundEvent.id).limit(1))
    assert conteggio is not None


@pytest.mark.asyncio
async def test_preview_markup_xss_salvata_senza_escaping(db_session):
    """Il modulo non fa escaping HTML (non e' il suo lavoro, minimizzazione
    SDD 12): verifica solo che markup ostile non causi un crash e resti
    intatto cosi' com'e' -- l'escaping e' responsabilita' di chi legge/renderizza."""
    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    contatto.chat_title = "Marco"
    await db_session.commit()

    ostile = "<script>alert(1)</script>"
    esito = await wa_reply_watcher.process_chat_row(
        db_session, tenant_id=tenant.id, wa_number_id=numero.id,
        row=_row("Marco", preview=ostile))
    assert esito["esito"] == "ignorato"

    eventi = (await db_session.execute(
        select(WaInboundEvent).where(WaInboundEvent.contact_id == contatto.id)
    )).scalars().all()
    assert len(eventi) == 1
    assert eventi[0].preview_text == ostile


@pytest.mark.asyncio
async def test_preview_unicode_ostile_non_crasha(db_session):
    """Emoji multi-byte (famiglia con ZWJ), RTL override (U+202E) e altri
    caratteri di controllo Unicode: match_contact/process_chat_row non devono
    crashare su testo che arriva cosi' com'e' dal DOM."""
    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    contatto.chat_title = "Marco"
    await db_session.commit()

    ostile = "Ciao \U0001F468‍\U0001F469‍\U0001F467‍\U0001F466 ‮EVIL‬ fine"
    esito = await wa_reply_watcher.process_chat_row(
        db_session, tenant_id=tenant.id, wa_number_id=numero.id,
        row=_row("Marco", preview=ostile))
    assert esito["esito"] == "ignorato"

    eventi = (await db_session.execute(
        select(WaInboundEvent).where(WaInboundEvent.contact_id == contatto.id)
    )).scalars().all()
    assert len(eventi) == 1
    assert eventi[0].preview_text == ostile


@pytest.mark.asyncio
async def test_preview_10000_caratteri_non_crasha(db_session):
    """preview_text e' Text (nessun limite di colonna): un insert enorme deve
    riuscire pulito, non crashare rumorosamente ne' troncare in silenzio."""
    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    contatto.chat_title = "Marco"
    await db_session.commit()

    ostile = "A" * 10_050
    esito = await wa_reply_watcher.process_chat_row(
        db_session, tenant_id=tenant.id, wa_number_id=numero.id,
        row=_row("Marco", preview=ostile))
    assert esito["esito"] == "ignorato"

    eventi = (await db_session.execute(
        select(WaInboundEvent).where(WaInboundEvent.contact_id == contatto.id)
    )).scalars().all()
    assert len(eventi) == 1
    assert eventi[0].preview_text == ostile
    assert len(eventi[0].preview_text) == 10_050


@pytest.mark.asyncio
async def test_title_null_byte_non_crasha_match_contact(db_session):
    """Verificato empiricamente (aiosqlite, connessione diretta): SQLite
    accetta un null byte embedded in una colonna TEXT senza sollevare --
    diverso da un C-string, la history string SQLite e' length-prefixed.
    match_contact quindi non crasha e non trova match (nessun contatto ha
    quel chat_title): nessuna eccezione, nessun errore. Comportamento non
    verificabile in questo ambiente su Postgres (produzione), che potrebbe
    comportarsi diversamente -- da tenere a mente se il canale WA passasse a
    Postgres per questa tabella."""
    tenant = await make_tenant(db_session)
    trovato, via = await wa_reply_watcher.match_contact(
        db_session, tenant.id, _row("Marco\x00Rossi"))
    assert trovato is None
    assert via == WaMatchedBy.none


@pytest.mark.asyncio
async def test_chat_title_nfc_vs_nfd_non_sono_trattati_come_uguali(db_session):
    """'café' in forma NFC (precomposta) vs NFD (e + accento combinante) sono
    byte-diversi. match_contact confronta a livello SQL (WHERE chat_title ==
    title), senza alcuna normalizzazione Unicode: documentiamo il
    comportamento reale, che diverge silenziosamente da quello 'atteso' da un
    umano (le due stringhe sembrano identiche a schermo). Rischio reale: se
    WhatsApp Web e la nostra scrittura del chat_title normalizzano in forme
    diverse, il contatto smette di matchare senza errori visibili -- non un
    match ambiguo, ma un falso 'nessun match'."""
    import unicodedata

    tenant = await make_tenant(db_session)
    nfc = unicodedata.normalize("NFC", "café")
    nfd = unicodedata.normalize("NFD", "café")
    assert nfc != nfd  # per costruzione: byte diversi, stessa resa a schermo

    contatto = await make_contact(db_session, tenant)
    contatto.chat_title = nfc
    await db_session.commit()

    trovato_nfc, via_nfc = await wa_reply_watcher.match_contact(db_session, tenant.id, _row(nfc))
    trovato_nfd, via_nfd = await wa_reply_watcher.match_contact(db_session, tenant.id, _row(nfd))

    assert trovato_nfc.id == contatto.id
    assert via_nfc == WaMatchedBy.chat_title
    # Comportamento reale, non quello "intuitivo": la forma diversa NON
    # matcha, silenziosamente (nessun match ambiguo rilevato ne' segnalato).
    assert trovato_nfd is None
    assert via_nfd == WaMatchedBy.none


# ---------------------------------------------------------------------------
# 3) Numero ostile / macchina a stati
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_number_id_inesistente(db_session):
    """Gia' presumibilmente coperto altrove (numero_non_attivo): verificato
    qui esplicitamente come richiesto dalla batteria adversarial."""
    esito = await wa_reply_watcher.scan_number(str(uuid.uuid4()))
    assert esito["motivo"] == "numero_non_attivo"
    assert esito["scansionate"] == 0


@pytest.mark.asyncio
async def test_numero_passa_a_retired_durante_lo_scan(db_session, monkeypatch):
    """scan_number legge lo stato del numero UNA volta (wa_reply_watcher.py,
    prima del blocco try/lock) e non lo ricontrolla dopo aver acquisito il
    lock ne' prima di aprire il browser (wa_reply_watcher.py righe ~248-260).
    Simulo un numero che passa a 'retired' esattamente nella finestra fra le
    due fasi (side_effect sul mock-browser).

    GAP REALE ma INNOCUO nell'immediato: il codice prosegue lo scan comunque
    (nessuna eccezione, nessuna scrittura sporca) perche' scan_chat_list()
    ritorna una lista vuota nel mock e non c'e' nulla da processare. Il danno
    potenziale (uno scan che legge/scrive per un numero appena ritirato,
    magari con una sessione WA gia' disconnessa dal cliente) dipenderebbe dal
    comportamento REALE di WhatsApp Web in quello stato, non testabile qui
    senza un browser vero -- segnalato, non corretto (fuori scope M4)."""
    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    await db_session.commit()

    class _BrowserCheRitira:
        def __call__(self, number_id, headless=True, proxy_url=None):
            return self

        async def __aenter__(self):
            async with AsyncSessionLocal() as db:
                await db.execute(update(WaNumber).where(WaNumber.id == numero.id)
                                 .values(status=WaNumberStatus.retired))
                await db.commit()

            class _Context:
                async def new_page(self):
                    class _Page:
                        async def goto(self, *a, **k):
                            pass
                    return _Page()
            return _Context()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(wa_reply_watcher, "_open_wa_browser", _BrowserCheRitira())
    monkeypatch.setattr(wa_reply_watcher, "WhatsAppWebPage", _PomVuoto)
    _lock_profilo_libero(monkeypatch)
    monkeypatch.setattr(wa_reply_watcher.bot_state_service, "is_wa_halted", _mai_halted)

    esito = await wa_reply_watcher.scan_number(numero.id)
    assert esito["motivo"] is None  # nessun crash, nessuna eccezione propagata
    assert esito["scansionate"] == 0

    await db_session.refresh(numero)
    assert numero.status == WaNumberStatus.retired  # conferma: il cambio di stato e' avvenuto per davvero


@pytest.mark.asyncio
@pytest.mark.xfail(
    reason="ADVERSARIALE, difetto reale confermato: race nel dedup di "
           "process_chat_row (wa_reply_watcher.py righe 149-164, SELECT poi "
           "INSERT+COMMIT senza lock) sotto concorrenza VERA. Non e' "
           "raggiungibile dai path di produzione attuali (process_chat_row "
           "e' chiamato solo in sequenza dentro il for-loop di scan_number, "
           "mai in parallelo), quindi non blocca il merge -- ma resta un "
           "difetto reale, xfail(strict=True) cosi' la CI resta verde SENZA "
           "nascondere la scoperta: se qualcuno lo corregge, il test inizia "
           "a passare e xfail(strict=True) lo segnala come XPASS da "
           "declassare a test normale.",
    strict=True,
)
async def test_doppio_stop_preview_identica_race_dedup(db_session):
    """ADVERSARIALE, atteso possa FALLIRE: due process_chat_row veri (sessioni
    DB separate, come farebbero due scan_number concorrenti) sullo stesso
    contatto con la STESSA preview STOP, in asyncio.gather reale.

    _ultima_preview_vista fa una SELECT poi, piu' sotto, un INSERT+COMMIT
    (wa_reply_watcher.py, process_chat_row righe 149-164): fra le due non c'e'
    alcun lock ne' alcuna constraint DB che impedisca a due coroutine di
    superare ENTRAMBE il check di dedup prima che una delle due abbia
    scritto. Con due sessioni distinte e asyncio reale, questo e' esattamente
    quello che succede.

    # ADVERSARIALE: se l'asserzione sotto fallisce, scopre una race reale nel
    # dedup di wa_reply_watcher.process_chat_row (righe 149-164): due
    # scansioni concorrenti sullo stesso numero (es. un reply-scan e un
    # secondo lanciato manualmente/per-retry) possono inserire DUE
    # wa_inbound_events per la stessa risposta e, piu' grave, chiamare
    # persist_wa_optout DUE volte in parallelo sullo stesso contatto. NON
    # correggere il codice di produzione per questo test -- e' materiale per
    # il team lead, fuori scope M4 (adversarial, non bugfix)."""
    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    contatto.chat_title = "Marco"
    await db_session.commit()

    riga = _row("Marco", preview="stop, ho detto stop")

    async def _process_in_new_session():
        async with AsyncSessionLocal() as db:
            return await wa_reply_watcher.process_chat_row(
                db, tenant_id=tenant.id, wa_number_id=numero.id, row=riga)

    r1, r2 = await asyncio.gather(_process_in_new_session(), _process_in_new_session())

    async with AsyncSessionLocal() as db:
        eventi = (await db.execute(
            select(WaInboundEvent).where(WaInboundEvent.contact_id == contatto.id)
        )).scalars().all()

    # Invariante attesa: una sola scrittura per la stessa preview, anche sotto
    # concorrenza reale. Vedi commento ADVERSARIALE sopra se questo fallisce.
    assert len(eventi) == 1, (
        f"ADVERSARIALE: dedup violato sotto concorrenza reale -- {len(eventi)} eventi "
        f"invece di 1 (esiti: {r1['esito']!r}, {r2['esito']!r}). Difetto reale in "
        "wa_reply_watcher.process_chat_row righe 149-164 (SELECT-poi-INSERT senza lock), "
        "non un bug del test. Non correggere qui: segnalato al team lead."
    )


# ---------------------------------------------------------------------------
# 4) WA_SEND_ENABLED irrilevante per il watcher
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wa_send_enabled_false_non_blocca_il_watcher(db_session, monkeypatch):
    """Vincolo esplicito del piano (gia' verificato via grep dai reviewer, mai
    con un test dedicato): il solo gate del reply-watcher e'
    bot_state_service.is_wa_halted(). settings.wa_send_enabled riguarda
    SOLO l'invio (wa_worker.esegui_mini_sessione, cancello 0) -- process_chat_row
    non lo legge nemmeno."""
    monkeypatch.setattr(settings, "wa_send_enabled", False)

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    contatto.chat_title = "Marco"
    await db_session.commit()

    esito = await wa_reply_watcher.process_chat_row(
        db_session, tenant_id=tenant.id, wa_number_id=numero.id,
        row=_row("Marco", preview="si mi interessa"))
    assert esito["esito"] == "ignorato"


@pytest.mark.asyncio
async def test_wa_send_enabled_false_non_blocca_scan_number(db_session, monkeypatch):
    """Stesso vincolo, verificato anche al livello scan_number (non solo
    process_chat_row): con WA_SEND_ENABLED=false lo scan deve completare
    normalmente."""
    monkeypatch.setattr(settings, "wa_send_enabled", False)

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    contatto = await make_contact(db_session, tenant)
    contatto.chat_title = "Marco"
    await db_session.commit()

    righe_finte = [_row("Marco", preview="ciao", unread=1)]

    class _PomConRighe:
        def __init__(self, page):
            pass
        async def session_state(self):
            return "logged_in"
        async def scan_chat_list(self):
            return righe_finte

    monkeypatch.setattr(wa_reply_watcher, "WhatsAppWebPage", _PomConRighe)
    _lock_profilo_libero(monkeypatch)
    monkeypatch.setattr(wa_reply_watcher.bot_state_service, "is_wa_halted", _mai_halted)

    class _ContextFinto:
        async def new_page(self):
            class _PageFinta:
                async def goto(self, *a, **k):
                    pass
            return _PageFinta()

    class _BrowserCtx:
        def __call__(self, number_id, headless=True, proxy_url=None):
            return self
        async def __aenter__(self):
            return _ContextFinto()
        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(wa_reply_watcher, "_open_wa_browser", _BrowserCtx())

    esito = await wa_reply_watcher.scan_number(numero.id)
    assert esito["motivo"] is None
    assert esito["scansionate"] == 1


# ---------------------------------------------------------------------------
# 5) Invarianti di dominio via SQL a fine run
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invarianti_dominio_dopo_operazioni_miste(db_session):
    """Sequenza mista (un contatto opta out, uno risponde, uno viene
    ignorato), poi tre query SQL DIRETTE (non asserzioni ORM in memoria) sul
    DB per verificare le invarianti di dominio."""
    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)

    contatto_optout = await make_contact(db_session, tenant, e164="+393350000001")
    contatto_optout.chat_title = "OptOutTest"
    campagna_a, _ = await make_campaign(db_session, tenant, numero, name="Camp A",
                                        status=WaCampaignStatus.running)
    await make_campaign_contact(db_session, campagna_a, contatto_optout,
                                status=WaContactStatus.in_sequence, current_step=0)

    contatto_replied = await make_contact(db_session, tenant, e164="+393350000002")
    contatto_replied.chat_title = "RepliedTest"
    campagna_b, _ = await make_campaign(db_session, tenant, numero, name="Camp B",
                                        status=WaCampaignStatus.running)
    await make_campaign_contact(db_session, campagna_b, contatto_replied,
                                status=WaContactStatus.completed, current_step=0)

    contatto_ignorato = await make_contact(db_session, tenant, e164="+393350000003")
    contatto_ignorato.chat_title = "IgnoratoTest"
    # nessuna riga wa_campaign_contacts: nessuna campagna a cui attribuire nulla

    await db_session.commit()

    await wa_reply_watcher.process_chat_row(
        db_session, tenant_id=tenant.id, wa_number_id=numero.id,
        row=_row("OptOutTest", preview="basta scrivermi, stop"))
    await wa_reply_watcher.process_chat_row(
        db_session, tenant_id=tenant.id, wa_number_id=numero.id,
        row=_row("RepliedTest", preview="si mi interessa"))
    await wa_reply_watcher.process_chat_row(
        db_session, tenant_id=tenant.id, wa_number_id=numero.id,
        row=_row("IgnoratoTest", preview="ok grazie mille"))
    await wa_reply_watcher.process_chat_row(
        db_session, tenant_id=tenant.id, wa_number_id=numero.id,
        row=_row("Sconosciuto", preview="chi sei"))  # non_associato

    # Le tre query sotto sono scoped al tenant di QUESTO test: il DB di test
    # e' condiviso fra i file della suite (contratto §7.4), una COUNT(*)
    # senza filtro conterebbe anche righe lasciate da altri test.

    # 1) Nessuna riga wa_campaign_contacts 'replied' con next_action_at NON NULL
    orfani_replied = await db_session.scalar(text(
        "SELECT COUNT(*) FROM wa_campaign_contacts cc "
        "JOIN wa_campaigns camp ON cc.campaign_id = camp.id "
        "WHERE camp.tenant_id = :tenant_id "
        "AND cc.status = 'replied' AND cc.next_action_at IS NOT NULL"
    ), {"tenant_id": tenant.id})
    assert orfani_replied == 0

    # 2) Nessun wa_contacts opted_out=1 con do_not_contact=0
    optout_incoerenti = await db_session.scalar(text(
        "SELECT COUNT(*) FROM wa_contacts "
        "WHERE tenant_id = :tenant_id AND opted_out = 1 AND do_not_contact = 0"
    ), {"tenant_id": tenant.id})
    assert optout_incoerenti == 0

    # 3) Nessuna riga wa_inbound_events con contact_id NOT NULL che referenzia
    #    un contatto inesistente (integrita' referenziale via LEFT JOIN diretto).
    orfani_eventi = await db_session.scalar(text(
        "SELECT COUNT(*) FROM wa_inbound_events e "
        "LEFT JOIN wa_contacts c ON e.contact_id = c.id "
        "WHERE e.tenant_id = :tenant_id "
        "AND e.contact_id IS NOT NULL AND c.id IS NULL"
    ), {"tenant_id": tenant.id})
    assert orfani_eventi == 0


@pytest.mark.asyncio
async def test_fk_integrity_rifiuta_contact_id_inesistente(db_session):
    """L'invariante 3 sopra non e' un caso: PRAGMA foreign_keys=ON e' attivo
    su OGNI connessione SQLite (app/database.py, listener globale
    sull'Engine, condiviso da tutti gli engine di test). Verifica diretta,
    adversariale: un tentativo di inserire un wa_inbound_events con un
    contact_id che non esiste deve essere RIFIUTATO dal DB, non
    silenziosamente accettato."""
    from sqlalchemy.exc import IntegrityError

    tenant = await make_tenant(db_session)
    numero = await make_number(db_session, tenant)
    await db_session.commit()

    db_session.add(WaInboundEvent(
        tenant_id=tenant.id, wa_number_id=numero.id,
        contact_id=str(uuid.uuid4()),  # non esiste nessun contatto con questo id
        preview_text="x", matched_by=WaMatchedBy.none, processed=True))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
