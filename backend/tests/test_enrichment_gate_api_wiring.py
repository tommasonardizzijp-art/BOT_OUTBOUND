"""Passo 4 / A.5 — il gate del livello e' davvero ATTACCATO ai TRE call-site del
ramo API (import_resolver.py, scraper.py x2), non solo alla funzione pura.

Perche' questo file esiste separato da test_enrichment_gate_contacts.py: quello
prova che `contatti_richiesti` (canale browser) e' giusta in isolamento; questo
prova che i punti di chiamata REALI del canale API la usano davvero. E' la stessa
lezione di test_professional_gate_wiring.py sul canale browser: una funzione pura
testata da sola non dimostra che il chiamante la invochi.

Semantica del livello (decisione Tommaso, passo 4 A.5): governa le RICHIESTE, non
i dati gia' arrivati gratis. Sul ramo API il payload della bio porta SEMPRE i campi
business (public_email/public_phone_number/contact_phone_number); il livello
decide solo se quei DUE campi vengono salvati. Il testo della bio, i link e il
whatsapp dedotto restano SEMPRE, a qualunque livello — non sono un campo dedicato,
non costano nessuna richiesta in piu'. Ogni test qui usa un payload con contatti
SIA nei campi business SIA nel testo della bio, con valori diversi apposta, cosi'
si vede quale sorgente e' davvero finita nel DB — un test che li' mettesse lo
stesso valore proverebbe ben poco.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus, ENRICHMENT_BIO, ENRICHMENT_CONTACTS
from app.models.follower import Follower, FollowerStatus
from app.models.imported_profile import ImportedProfile
from app.services import scraper

# Valori business e valori bio-testo DIVERSI apposta (vedi docstring del modulo).
_BIZ_EMAIL = "business@shop.example.com"
_BIZ_PHONE = "+393491234567"  # da public_phone_number=3491234567 + cc=39
_BIO_EMAIL = "bio@example.com"
_BIO_PHONE = "3331234567"  # regex sul testo, nessun paese -> nessun prefisso +


def _user_info(pk, username):
    """Payload instagrapi-shaped con contatti SIA nei campi business dedicati SIA
    nel testo della bio (valori diversi), piu' un external_url per bio_links."""
    return SimpleNamespace(
        pk=str(pk), username=username, full_name="Full Name",
        biography=f"Scrivici a {_BIO_EMAIL} o chiama il {_BIO_PHONE}",
        is_verified=False, follower_count=10, following_count=5,
        external_url="https://negozio.example.com/shop", bio_links=[], profile_pic_url=None,
        public_email=_BIZ_EMAIL, public_phone_number="3491234567",
        public_phone_country_code="39", contact_phone_number=None,
    )


class _FakePool:
    """pool.next() ritorna sempre la stessa coppia (account, client): basta per
    esercitare un singolo follower/batch, che e' tutto cio' che serve qui."""

    def __init__(self, account, client):
        self._sel = (account, client)

    def next(self, campaign):
        return self._sel

    async def save_sessions(self, db):
        pass


def _account():
    return SimpleNamespace(id="acc-gate", username="acc_gate", scrape_lookups_today=0,
                            scrape_lookups_date=None)


def _assert_gate(f, livello, attesi_campi_dedicati):
    """Contratto comune ai tre call-site: bio_links/external_url e i contatti
    dal TESTO restano SEMPRE; i campi business dedicati solo se il livello e'
    'contacts' (o assente/None, retrocompatibilita')."""
    assert f.biography, "la bio deve arrivare sempre, indipendentemente dal livello"
    assert f.bio_links, (
        f"livello={livello}: bio_links assente — non e' un campo dedicato, deve restare sempre"
    )
    if attesi_campi_dedicati:
        assert f.email == _BIZ_EMAIL, (
            f"livello={livello}: atteso il campo business (email={f.email!r}). "
            "Se e' quello del testo bio, il gate a 'contacts' non sta includendo i campi dedicati."
        )
        assert f.phone == _BIZ_PHONE, (
            f"livello={livello}: atteso il campo business (phone={f.phone!r})."
        )
    else:
        assert f.email == _BIO_EMAIL, (
            f"livello={livello}: atteso il contatto dal TESTO bio (email={f.email!r}). "
            "Se e' quello business, il gate NON sta escludendo i campi dedicati a questo livello."
        )
        assert f.phone == _BIO_PHONE, (
            f"livello={livello}: atteso il contatto dal TESTO bio (phone={f.phone!r})."
        )


# --------------------------------------------------------------------------- #
# Call-site 1/3 — scraper.py: fetch_and_store_bio (Fase Bio, motore API)
# --------------------------------------------------------------------------- #
async def _campagna_bio_con_follower(pk, username, livello):
    async with AsyncSessionLocal() as db:
        camp = Campaign(
            name=f"gate-api-bio-{username}", status=CampaignStatus.scraping,
            source_type="scrape", bio_engine="api", enrichment_level=livello,
            bio_fetch_delay_min=0, bio_fetch_delay_max=0,
        )
        db.add(camp)
        await db.flush()
        f = Follower(campaign_id=camp.id, ig_user_id=pk, username=username,
                     status=FollowerStatus.pending)
        db.add(f)
        await db.commit()
        return camp.id, f.id


@pytest.mark.asyncio
@pytest.mark.parametrize("livello, attesi_campi_dedicati", [
    (ENRICHMENT_CONTACTS, True),
    (ENRICHMENT_BIO, False),
    # NB: niente caso retrocompat (livello assente) qui. `enrichment_level` e'
    # NOT NULL con default lato colonna (migration 029): un Campaign() reale con
    # enrichment_level=None diventa 'none' al commit, non resta None — quindi il
    # ramo "campo assente" della funzione pura non e' raggiungibile da una riga
    # Campaign vera. E' gia' coperto in isolamento in test_enrichment_gate_contacts.py
    # e a livello di wiring in test_resolve_imports_retrocompat_livello_assente sotto
    # (oggetto in memoria, unico posto dove il ramo e' davvero raggiungibile).
])
async def test_fetch_and_store_bio_gate_solo_campi_dedicati(monkeypatch, livello, attesi_campi_dedicati):
    pk = 991100000001 + abs(hash(("bio", livello))) % 900000
    username = f"apibio{pk}"
    cid, fid = await _campagna_bio_con_follower(pk, username, livello)

    account = _account()
    pool = _FakePool(account, client=object())

    def fake_fetch(client_arg, ig_user_id):
        # asyncio.to_thread() chiama questa come funzione SINCRONA: se fosse
        # async ritornerebbe una coroutine mai awaitata.
        return _user_info(pk, username)

    monkeypatch.setattr("app.services.profile_lookup.fetch_profile_app_like", fake_fetch)
    monkeypatch.setattr(scraper, "upsert_lead", AsyncMock())  # scrittura lead, non sotto test

    async with AsyncSessionLocal() as db:
        camp = await db.get(Campaign, cid)
        f = await db.get(Follower, fid)
        outcome, used_account, err = await scraper.fetch_and_store_bio(f, camp, db, pool)

    assert outcome == "done", f"lookup non riuscita: {outcome} {err!r}"

    async with AsyncSessionLocal() as db:
        f = await db.get(Follower, fid)
        _assert_gate(f, livello, attesi_campi_dedicati)


# --------------------------------------------------------------------------- #
# Call-site 2/3 — scraper.py: _store_followers_batch (Fase Lista, motore API)
# --------------------------------------------------------------------------- #
async def _campagna_lista(livello):
    async with AsyncSessionLocal() as db:
        camp = Campaign(
            name=f"gate-api-lista-{livello}", status=CampaignStatus.scraping,
            source_type="scrape", bio_engine="api", enrichment_level=livello,
            bio_fetch_delay_min=0, bio_fetch_delay_max=0,
        )
        db.add(camp)
        await db.commit()
        return camp.id


@pytest.mark.asyncio
@pytest.mark.parametrize("livello, attesi_campi_dedicati", [
    (ENRICHMENT_CONTACTS, True),
    (ENRICHMENT_BIO, False),
    # Vedi la nota sopra in test_fetch_and_store_bio_...: nessun caso retrocompat,
    # stessa colonna NOT NULL con default.
])
async def test_store_followers_batch_gate_solo_campi_dedicati(monkeypatch, livello, attesi_campi_dedicati):
    pk = 992200000001 + abs(hash(("lista", livello))) % 900000
    username = f"apilista{pk}"
    cid = await _campagna_lista(livello)

    account = _account()
    client = SimpleNamespace(user_info_v1=lambda p: _user_info(pk, username))
    pool = _FakePool(account, client)

    user_short = SimpleNamespace(pk=pk, username=username, full_name="Full Name",
                                  is_private=False, profile_pic_url=None)

    monkeypatch.setattr(scraper, "upsert_lead", AsyncMock())

    async with AsyncSessionLocal() as db:
        camp = await db.get(Campaign, cid)
        stored = await scraper._store_followers_batch([user_short], camp, db, pool)

    assert stored == 1, "il follower non e' stato salvato (setup del test rotto, non il gate)"

    async with AsyncSessionLocal() as db:
        from sqlalchemy import select
        f = (await db.execute(
            select(Follower).where(Follower.campaign_id == cid, Follower.ig_user_id == pk)
        )).scalar_one()
        _assert_gate(f, livello, attesi_campi_dedicati)


# --------------------------------------------------------------------------- #
# Call-site 3/3 — import_resolver.py: resolve_imports (risoluzione import, motore API)
# --------------------------------------------------------------------------- #
class _FakePoolIR:
    """Come _FakePool ma con le superfici in piu' che resolve_imports usa attorno
    al loop vero e proprio: .size nel log di avvio, .release() nel finally."""

    size = 1

    def __init__(self, account, client):
        self._sel = (account, client)

    def next(self, campaign):
        return self._sel

    async def save_sessions(self, db):
        pass

    async def release(self):
        pass


async def _campagna_import_con_riga(pk, username, livello):
    async with AsyncSessionLocal() as db:
        camp = Campaign(
            name=f"gate-api-import-{username}", status=CampaignStatus.scraping,
            source_type="import", bio_engine="api", enrichment_level=livello,
            messaging_enabled=True, bio_fetch_delay_min=0, bio_fetch_delay_max=0,
            scrape_session_size=250,
        )
        db.add(camp)
        await db.flush()
        row = ImportedProfile(campaign_id=camp.id, raw_input=username,
                              username=username, status="pending")
        db.add(row)
        await db.commit()
        return camp.id


@pytest.mark.asyncio
@pytest.mark.parametrize("livello, attesi_campi_dedicati", [
    (ENRICHMENT_CONTACTS, True),
    (ENRICHMENT_BIO, False),
    # Niente caso retrocompat qui, stessa colonna NOT NULL delle altre due —
    # coperto a parte in test_resolve_imports_retrocompat_livello_assente sotto.
])
async def test_resolve_imports_gate_solo_campi_dedicati(monkeypatch, livello, attesi_campi_dedicati):
    """A differenza di una versione db=MagicMock() (verifica sull'oggetto passato a
    db.add() prima del commit), qui si pilota resolve_imports contro un Campaign/
    ImportedProfile REALI su DB e si rilegge il Follower dal DB dopo — prova la
    persistenza, non solo la costruzione dell'oggetto in memoria. Solo pool/client
    IG restano finti (non c'e' rete)."""
    import app.services.import_resolver as ir

    pk = 993300000001 + abs(hash(("import", livello))) % 900000
    username = f"apiimp{pk}"
    cid = await _campagna_import_con_riga(pk, username, livello)

    account = _account()
    client = SimpleNamespace(user_info_by_username_v1=lambda u: _user_info(pk, username))
    pool = _FakePoolIR(account, client)

    monkeypatch.setattr(ir, "upsert_lead", AsyncMock())
    monkeypatch.setattr(ir.ScrapingPool, "build", AsyncMock(return_value=pool))

    await ir.resolve_imports(cid)

    async with AsyncSessionLocal() as db:
        from sqlalchemy import select
        f = (await db.execute(
            select(Follower).where(Follower.campaign_id == cid, Follower.ig_user_id == pk)
        )).scalar_one()
        _assert_gate(f, livello, attesi_campi_dedicati)


class _FakeCtx:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *a):
        return False


def _result(value):
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    return r


@pytest.mark.asyncio
async def test_resolve_imports_retrocompat_livello_assente(monkeypatch):
    """Copre SOLO il ramo 'livello assente' della funzione pura dentro il
    call-site reale. Usa un Campaign IN MEMORIA (MagicMock) perche' una riga
    Campaign vera non puo' avere enrichment_level=None (colonna NOT NULL con
    default 'none', vedi il docstring di contatti_richiesti_dal_livello in
    campaign.py) — quindi qui NON si prova la persistenza su DB reale come nel
    test sopra, si prova solo che il call-site passi l'attributo giusto (assente)
    alla funzione pura e ne rispetti l'esito (procede come se fosse 'contacts')."""
    import app.services.import_resolver as ir
    from app.services.scraping_pool import ScrapingPool

    pk = 994400000001 + abs(hash("retrocompat")) % 900000
    username = f"apiimp{pk}"

    client = MagicMock()
    client.get_settings = MagicMock(return_value={})
    client.user_info_by_username_v1 = MagicMock(return_value=_user_info(pk, username))
    pool = ScrapingPool([
        {"account": SimpleNamespace(id="A", username="A", scrape_lookups_today=0,
                                    session_data=None, last_activity_at=None),
         "client": client, "slot_owned": False},
    ])

    campaign = SimpleNamespace(
        id="camp-gate-retro", source_type="import", status=CampaignStatus.scraping,
        bio_engine="api", enrichment_level=None,
        scrape_daily_limit=180, bio_fetch_delay_min=0, bio_fetch_delay_max=0,
        scrape_session_size=250, messaging_enabled=True,
        total_followers=0, messages_pending=0, scrape_outcome=None,
        scrape_completed_at=None, updated_at=None,
    )
    row = SimpleNamespace(username=username, status="pending", error=None, ig_user_id=None)

    db = MagicMock()
    db.refresh = AsyncMock()
    db.commit = AsyncMock()
    db.add = MagicMock()
    db.scalar = AsyncMock(return_value=1)
    # ordine execute: Campaign, reset 'resolving'->'pending', poi (riga pending, dup
    # Follower) per la sola riga, poi riga pending=None -> il loop termina.
    db.execute = AsyncMock(side_effect=[
        _result(campaign),
        _result(None),
        _result(row),
        _result(None),
        _result(None),
    ])

    monkeypatch.setattr(ir, "AsyncSessionLocal", lambda: _FakeCtx(db))
    monkeypatch.setattr(ir, "is_halted", AsyncMock(return_value=False))
    monkeypatch.setattr(ir, "increment_scrape_lookup", AsyncMock())
    monkeypatch.setattr(ir, "upsert_lead", AsyncMock())
    monkeypatch.setattr(ir.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(ir.ScrapingPool, "build", AsyncMock(return_value=pool))

    await ir.resolve_imports("camp-gate-retro")

    followers_added = [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], Follower)]
    assert len(followers_added) == 1, "il Follower non e' stato creato (setup del test rotto, non il gate)"
    f = followers_added[0]
    assert f.email == _BIZ_EMAIL, (
        "livello assente (None): deve procedere come prima dei livelli "
        f"(campo business atteso, trovato {f.email!r})"
    )
    assert f.phone == _BIZ_PHONE
    assert f.bio_links, "livello assente: i link in bio devono comunque restare"
