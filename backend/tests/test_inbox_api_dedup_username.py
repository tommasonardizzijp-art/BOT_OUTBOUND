"""Il percorso API dell'inbox riconosce i contatti raccolti dal browser.

Il difetto (misurato su prod il 21/08/2026, campagna AV X @michele.carozza): il
canale browser non conosce il pk Instagram e salva una TARGA PROVVISORIA negativa
(`inbox_browser/targa.py`), mentre il percorso API deduplica solo su `ig_user_id`.
Le due chiavi non combaciano mai, quindi 32 delle 34 righe create dalle prime
chiamate API erano doppioni di contatti che il browser aveva gia' preso: righe
gemelle con lo stesso username, una targa finta e una vera.

La rete che mancava e' questa: prima di inserire, si guarda anche l'USERNAME. Se
combacia con una riga a targa provvisoria, l'API ha in mano proprio il dato che
mancava — il pk vero — e quindi PROMUOVE la riga esistente invece di crearne una
seconda.
"""
import pytest

from app.services.scrape_inbox import classifica_pagina


def _targhe(coppie):
    """{username normalizzato: ig_user_id} come lo costruisce il loop."""
    return dict(coppie)


def test_username_con_targa_provvisoria_viene_promosso_non_inserito():
    esito = classifica_pagina(
        [(555, "mario_shop")],
        existing_ids=set(),
        targa_per_username=_targhe([("mario_shop", -8347)]),
    )
    assert esito.nuovi == []
    assert esito.promozioni == [(555, "mario_shop", "mario_shop")]


def test_username_con_targa_reale_diversa_e_una_persona_diversa():
    """Username riassegnato dopo un rename: la riga vecchia ha una targa VERA, il
    pk nuovo e' di un altro profilo. Si inserisce (sono due persone) e si segnala."""
    esito = classifica_pagina(
        [(999, "negozio")],
        existing_ids=set(),
        targa_per_username=_targhe([("negozio", 111)]),
    )
    assert esito.nuovi == [(999, "negozio")]
    assert esito.promozioni == []
    assert esito.collisioni_username == ["negozio"]


def test_confronto_sullo_username_normalizzato():
    """In DB alcuni username sono salvati con la chiocciola e in maiuscolo
    (vedi targa.normalizza_username): devono combaciare lo stesso."""
    esito = classifica_pagina(
        [(555, "Mario_Shop")],
        existing_ids=set(),
        targa_per_username=_targhe([("mario_shop", -8347)]),
    )
    assert esito.promozioni == [(555, "mario_shop", "Mario_Shop")]
    assert esito.nuovi == []


def test_stesso_username_due_volte_nella_pagina_promuove_una_volta_sola():
    """Due thread diversi che portano allo stesso username: una sola riga da
    promuovere, l'altro pk e' un'altra persona e va inserito."""
    esito = classifica_pagina(
        [(555, "mario_shop"), (556, "mario_shop")],
        existing_ids=set(),
        targa_per_username=_targhe([("mario_shop", -8347)]),
    )
    assert esito.promozioni == [(555, "mario_shop", "mario_shop")]
    assert esito.nuovi == [(556, "mario_shop")]


def test_pk_gia_in_lista_non_produce_ne_inserimenti_ne_promozioni():
    esito = classifica_pagina(
        [(555, "mario_shop")],
        existing_ids={555},
        targa_per_username=_targhe([("mario_shop", 555)]),
    )
    assert esito.nuovi == []
    assert esito.promozioni == []
    assert esito.gia_presenti == 1


def test_username_sconosciuto_resta_un_contatto_nuovo():
    esito = classifica_pagina(
        [(555, "mai_visto")],
        existing_ids=set(),
        targa_per_username={},
    )
    assert esito.nuovi == [(555, "mai_visto")]
    assert esito.promozioni == []
    assert esito.collisioni_username == []


@pytest.mark.parametrize("username", ["", "   ", "@", None])
def test_username_inutilizzabile_non_promuove_mai(username):
    """Senza username non si puo' riconoscere nulla: si tratta come nuovo, mai
    come promozione (promuovere sulla riga sbagliata scriverebbe il pk di uno
    sconosciuto su un contatto gia' lavorato)."""
    esito = classifica_pagina(
        [(555, username)],
        existing_ids=set(),
        targa_per_username=_targhe([("", -1), ("mario_shop", -8347)]),
    )
    assert esito.promozioni == []
    assert esito.nuovi == [(555, username)]


# ═══════════════════════════════════════════════════════════════════
# Loop: run_inbox_list contro un DB vero (SQLite) e una sorgente scriptata
# ═══════════════════════════════════════════════════════════════════
import asyncio

from sqlalchemy import func, select

from app.config import settings
from app.models.campaign import Campaign, CampaignStatus
from app.models.follower import Follower, FollowerStatus
from app.services.inbox_source import InboxPage
from test_scrape_inbox_adversarial import (          # noqa: E402  (harness condivisa)
    _CountingSource, _inject_source, _run_inbox_list, _setup_inbox_db,
)


def _semina_follower(session_factory, campaign_id, **kwargs):
    async def _go():
        async with session_factory() as db:
            db.add(Follower(campaign_id=campaign_id, **kwargs))
            await db.commit()
    asyncio.run(_go())


def _leggi_follower(session_factory, campaign_id):
    async def _go():
        async with session_factory() as db:
            righe = (await db.execute(
                select(Follower).where(Follower.campaign_id == campaign_id)
            )).scalars().all()
            campaign = await db.get(Campaign, campaign_id)
            return righe, campaign
    return asyncio.run(_go())


def test_il_loop_promuove_la_riga_del_browser_invece_di_duplicarla(monkeypatch):
    """Il caso reale del 21/08: la riga c'e' gia', presa dal browser, con targa
    provvisoria. L'API deve scriverci il pk vero, NON creare la seconda riga."""
    pages = [
        InboxPage(participants=[(555, "mario_shop")], cursor="c1", exhausted=False),
        InboxPage(participants=[], cursor=None, exhausted=True),
    ]
    session_factory, campaign_id, cleanup = _setup_inbox_db(monkeypatch, pages)
    try:
        _semina_follower(
            session_factory, campaign_id,
            ig_user_id=-8347, username="mario_shop", full_name="Mario",
            status=FollowerStatus.pending, source_channel="browser",
        )
        _run_inbox_list(session_factory, campaign_id)
        righe, campaign = _leggi_follower(session_factory, campaign_id)
        assert len(righe) == 1, f"attesa 1 riga, trovate {len(righe)}: {[(r.username, r.ig_user_id) for r in righe]}"
        assert righe[0].ig_user_id == 555, "la targa provvisoria non e' stata promossa"
        assert righe[0].full_name == "Mario", "i dati del browser non vanno persi"
        assert righe[0].source_channel == "browser"
        assert campaign.total_followers == 1, "il contatore deve contare le righe vere"
    finally:
        cleanup()


def test_una_promozione_non_gonfia_il_contatore(monkeypatch):
    """Due contatti gia' presi dal browser + uno nuovo: il totale e' 3, non 5."""
    pages = [
        InboxPage(
            participants=[(1, "uno"), (2, "due"), (3, "tre")], cursor="c1", exhausted=False
        ),
        InboxPage(participants=[], cursor=None, exhausted=True),
    ]
    session_factory, campaign_id, cleanup = _setup_inbox_db(monkeypatch, pages)
    try:
        _semina_follower(session_factory, campaign_id, ig_user_id=-11, username="uno",
                         status=FollowerStatus.pending, source_channel="browser")
        _semina_follower(session_factory, campaign_id, ig_user_id=-22, username="due",
                         status=FollowerStatus.pending, source_channel="browser")
        _run_inbox_list(session_factory, campaign_id)
        righe, campaign = _leggi_follower(session_factory, campaign_id)
        assert len(righe) == 3
        assert campaign.total_followers == 3
        assert sorted(r.ig_user_id for r in righe) == [1, 2, 3]
    finally:
        cleanup()


def test_il_fondo_raggiunto_alza_il_flag_e_azzera_il_cursore(monkeypatch):
    """Il fondo lo dichiara solo has_older=False (bottom_confirmed)."""
    pages = [InboxPage(participants=[(7, "sette")], cursor=None, exhausted=True,
                       bottom_confirmed=True)]
    session_factory, campaign_id, cleanup = _setup_inbox_db(monkeypatch, pages)
    try:
        _run_inbox_list(session_factory, campaign_id)
        _, campaign = _leggi_follower(session_factory, campaign_id)
        assert campaign.inbox_bottom_reached is True
        assert campaign.inbox_deep_cursor is None
        assert campaign.scrape_cursor is None
    finally:
        cleanup()


def test_una_pagina_senza_cursore_non_dichiara_finito_l_inbox(monkeypatch):
    """`exhausted` scatta anche solo perche' manca oldest_cursor (payload troncato,
    blip). Bastava quello per alzare un interruttore PERMANENTE: da li' in poi la
    campagna non sarebbe piu' scesa e il fondo — la ragione per cui si usa l'API —
    sarebbe irraggiungibile. Il giro si ferma, il flag non si alza."""
    pages = [InboxPage(participants=[(7, "sette")], cursor=None, exhausted=True,
                       bottom_confirmed=False)]
    session_factory, campaign_id, cleanup = _setup_inbox_db(monkeypatch, pages)
    try:
        _run_inbox_list(session_factory, campaign_id)
        _, campaign = _leggi_follower(session_factory, campaign_id)
        assert campaign.inbox_bottom_reached is False
        assert campaign.status == CampaignStatus.ready
    finally:
        cleanup()


def test_a_giro_chiuso_scrape_cursor_torna_vuoto(monkeypatch):
    """campaign_control legge scrape_cursor come "Fase Lista interrotta a meta'":
    lasciandolo valorizzato, una ripresa tornerebbe SEMPRE in lista e la Fase Bio
    non partirebbe mai. La frontiera della discesa vive in inbox_deep_cursor."""
    pages = [
        InboxPage(participants=[(1, "uno")], cursor="c1", exhausted=False),
        InboxPage(participants=[], cursor=None, exhausted=True, bottom_confirmed=False),
    ]
    session_factory, campaign_id, cleanup = _setup_inbox_db(monkeypatch, pages)
    try:
        _run_inbox_list(session_factory, campaign_id)
        _, campaign = _leggi_follower(session_factory, campaign_id)
        assert campaign.scrape_cursor is None, "il giro e' chiuso: niente lista a meta'"
        assert campaign.inbox_deep_cursor == "c1", "la frontiera deve sopravvivere"
    finally:
        cleanup()


def test_in_modalita_cima_il_cursore_non_viene_mai_salvato(monkeypatch):
    """A fondo raggiunto il giro riparte sempre dall'alto: salvare il cursore
    farebbe credere a campaign_control che ci sia una lista interrotta a meta'."""
    session_factory, campaign_id, cleanup = _setup_inbox_db(
        monkeypatch, [], bottom_reached=True
    )
    try:
        src = _CountingSource(
            lambda n: InboxPage(participants=[], cursor=f"c{n}", exhausted=False)
        )
        _inject_source(monkeypatch, src)
        _run_inbox_list(session_factory, campaign_id)
        _, campaign = _leggi_follower(session_factory, campaign_id)
        assert campaign.scrape_cursor is None
        assert src.calls == settings.inbox_empty_page_stop
    finally:
        cleanup()


def test_in_discesa_le_pagine_di_soli_gia_noti_non_fermano_il_giro(monkeypatch):
    """Il cuore della modifica: scendendo verso i thread vecchi si attraversano
    decine di pagine di gente gia' raccolta dal browser. Il giro deve arrivare in
    fondo, non fermarsi al primo tratto senza nuovi."""
    noti = [(i, f"u{i}") for i in range(1, 21)]

    def page_fn(n):
        if n <= settings.inbox_empty_page_stop + 2:
            return InboxPage(participants=noti, cursor=f"c{n}", exhausted=False)
        return InboxPage(participants=[(999, "finalmente_nuovo")], cursor=None,
                         exhausted=True, bottom_confirmed=True)

    session_factory, campaign_id, cleanup = _setup_inbox_db(monkeypatch, [])
    try:
        for pk, u in noti:
            _semina_follower(session_factory, campaign_id, ig_user_id=pk, username=u,
                             status=FollowerStatus.pending)
        src = _CountingSource(page_fn)
        _inject_source(monkeypatch, src)
        _run_inbox_list(session_factory, campaign_id)
        righe, campaign = _leggi_follower(session_factory, campaign_id)
        assert 999 in [r.ig_user_id for r in righe], "il giro si e' fermato prima del fondo"
        assert campaign.inbox_bottom_reached is True
    finally:
        cleanup()


def test_cursore_fermo_interrompe_il_giro(monkeypatch):
    """IG risponde ma la finestra non si sposta: continuare significherebbe
    richiedere la stessa pagina all'infinito."""
    session_factory, campaign_id, cleanup = _setup_inbox_db(monkeypatch, [])
    try:
        src = _CountingSource(
            lambda n: InboxPage(participants=[], cursor="sempre_uguale", exhausted=False)
        )
        _inject_source(monkeypatch, src)
        result = _run_inbox_list(session_factory, campaign_id)
        _, campaign = _leggi_follower(session_factory, campaign_id)
        assert result is None
        assert campaign.status == CampaignStatus.ready
        assert src.calls == 2, f"atteso stop alla seconda pagina identica, {src.calls}"
        assert campaign.inbox_bottom_reached is False, "cursore fermo non e' il fondo"
    finally:
        cleanup()


def test_la_campagna_ricorda_di_aver_toccato_il_fondo():
    """Il flag distingue le due modalita' della Fase Lista inbox API. Nasce False
    (nessuna campagna esistente ha mai toccato il fondo) e non e' nullable: un
    None qui renderebbe ambiguo 'non lo so' contro 'non ancora'."""
    assert Campaign.__table__.c.inbox_bottom_reached.nullable is False
    assert Campaign.__table__.c.inbox_bottom_reached.server_default is not None


def test_promuove_anche_la_riga_salvata_con_la_chiocciola(monkeypatch):
    """In DB certi username stanno con la chiocciola o in maiuscolo (lo dice
    targa.normalizza_username). La promozione deve colpire QUELLA riga: cercarla
    con una WHERE sullo username grezzo non la troverebbe, e il contatto —
    classificato come promozione, quindi mai inserito — sparirebbe in silenzio."""
    pages = [
        InboxPage(participants=[(555, "mario_shop")], cursor="c1", exhausted=False),
        InboxPage(participants=[], cursor=None, exhausted=True),
    ]
    session_factory, campaign_id, cleanup = _setup_inbox_db(monkeypatch, pages)
    try:
        _semina_follower(
            session_factory, campaign_id,
            ig_user_id=-8347, username="@Mario_Shop", full_name="Mario",
            status=FollowerStatus.pending, source_channel="browser",
        )
        _run_inbox_list(session_factory, campaign_id)
        righe, campaign = _leggi_follower(session_factory, campaign_id)
        assert len(righe) == 1, f"attesa 1 riga, trovate {[(r.username, r.ig_user_id) for r in righe]}"
        assert righe[0].ig_user_id == 555
        assert campaign.total_followers == 1
    finally:
        cleanup()


def test_due_pk_sullo_stesso_username_non_fanno_tre_righe(monkeypatch):
    """Stessa pagina, stesso username, due pk diversi: uno e' la persona gia' in
    lista (promozione), l'altro e' un'altra persona (inserimento). Devono restare
    DUE righe.

    Se gli inserimenti girassero prima delle promozioni, l'inserimento toglierebbe
    di mezzo la riga da promuovere e si finirebbe con tre righe per due persone:
    la provvisoria mai promossa piu' le due reali — cioe' il doppione che questo
    codice esiste per chiudere."""
    pages = [
        InboxPage(participants=[(555, "mario_shop"), (556, "mario_shop")],
                  cursor="c1", exhausted=False),
        InboxPage(participants=[], cursor=None, exhausted=True, bottom_confirmed=True),
    ]
    session_factory, campaign_id, cleanup = _setup_inbox_db(monkeypatch, pages)
    try:
        _semina_follower(
            session_factory, campaign_id,
            ig_user_id=-8347, username="mario_shop", full_name="Mario",
            status=FollowerStatus.pending, source_channel="browser",
        )
        _run_inbox_list(session_factory, campaign_id)
        righe, campaign = _leggi_follower(session_factory, campaign_id)
        targhe = sorted(r.ig_user_id for r in righe)
        assert targhe == [555, 556], f"attese due righe reali, trovate {targhe}"
        assert campaign.total_followers == 2
    finally:
        cleanup()
