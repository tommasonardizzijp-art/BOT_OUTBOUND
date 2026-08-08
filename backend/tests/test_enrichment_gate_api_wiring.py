"""Passo 4 / A.5 — il gate del livello e' davvero ATTACCATO ai TRE call-site del
ramo API (import_resolver.py, scraper.py x2), non solo alla funzione pura.

Perche' questo file esiste separato da test_enrichment_gate_contacts.py: quello
prova che `contatti_richiesti` (canale browser) e' giusta in isolamento; questo
prova che i punti di chiamata REALI del canale API la usano davvero. E' la stessa
lezione di test_professional_gate_wiring.py sul canale browser: una funzione pura
testata da sola non dimostra che il chiamante la invochi. Sul ramo API il gate non
salva nessuna richiesta IG (i contatti sono gia' nel payload della bio) — decide
solo se finiscono nel DB, quindi qui l'unico modo per accorgersi di una regressione
e' guardare cosa viene scritto sul Follower, non contare le chiamate di rete.

Ogni test verifica anche che `bio_links`/`external_url` restino salvati a livello
'bio': non sono un contatto, sono cio' che quel livello promette ("bio/follower/
link"). E' la distinzione tra "gate del contatto" e "gate di tutto il parsing" —
un fix del gate che gatasse anche i link sarebbe un secondo bug, non la stessa cosa.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStatus, ENRICHMENT_BIO, ENRICHMENT_CONTACTS
from app.models.follower import Follower, FollowerStatus
from app.models.imported_profile import ImportedProfile
from app.services import scraper


def _user_info(pk, username):
    """Payload instagrapi-shaped con un contatto SOLO nel testo bio (regex source,
    non dipende dai nomi esatti dei campi business_* di IG) + un external_url,
    cosi' bio_links e' sempre popolabile indipendentemente dal livello."""
    return SimpleNamespace(
        pk=str(pk), username=username, full_name="Full Name",
        biography="Contattaci: negozio@example.com",
        is_verified=False, follower_count=10, following_count=5,
        external_url="https://negozio.example.com/shop", bio_links=[], profile_pic_url=None,
        public_email=None, public_phone_number=None,
        public_phone_country_code=None, contact_phone_number=None,
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


def _assert_gate_e_link(f, livello, attesi_contatti):
    assert f.biography, "la bio deve arrivare sempre, indipendentemente dal livello"
    ha_contatti = bool(f.email or f.phone)
    assert ha_contatti is attesi_contatti, (
        f"livello={livello}: contatti salvati={ha_contatti}, attesi={attesi_contatti}. "
        "Se sono SEMPRE presenti il gate non e' collegato a questo call-site."
    )
    assert f.bio_links, (
        f"livello={livello}: bio_links assente. Il link in bio NON e' un contatto — "
        "deve restare anche a livello 'bio' (il gate ha gatato troppo)."
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
@pytest.mark.parametrize("livello, attesi_contatti", [
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
async def test_fetch_and_store_bio_salva_contatti_solo_a_livello_contacts(monkeypatch, livello, attesi_contatti):
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
        _assert_gate_e_link(f, livello, attesi_contatti)


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
@pytest.mark.parametrize("livello, attesi_contatti", [
    (ENRICHMENT_CONTACTS, True),
    (ENRICHMENT_BIO, False),
    # Vedi la nota sopra in test_fetch_and_store_bio_...: nessun caso retrocompat,
    # stessa colonna NOT NULL con default.
])
async def test_store_followers_batch_salva_contatti_solo_a_livello_contacts(monkeypatch, livello, attesi_contatti):
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
        _assert_gate_e_link(f, livello, attesi_contatti)


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
@pytest.mark.parametrize("livello, attesi_contatti", [
    (ENRICHMENT_CONTACTS, True),
    (ENRICHMENT_BIO, False),
    # Niente caso retrocompat qui, stessa colonna NOT NULL delle altre due —
    # coperto a parte in test_resolve_imports_retrocompat_livello_assente sotto.
])
async def test_resolve_imports_salva_contatti_solo_a_livello_contacts(monkeypatch, livello, attesi_contatti):
    """A differenza della versione precedente di questo test (db=MagicMock(),
    verifica sull'oggetto passato a db.add() prima del commit), qui si pilota
    resolve_imports contro un Campaign/ImportedProfile REALI su DB e si rilegge
    il Follower dal DB dopo — prova la persistenza, non solo la costruzione
    dell'oggetto in memoria. Solo pool/client IG restano finti (non c'e' rete)."""
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
        _assert_gate_e_link(f, livello, attesi_contatti)


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
    alla funzione pura e ne rispetti l'esito."""
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
    assert bool(f.email or f.phone) is True, (
        "livello assente (None): deve procedere come prima dei livelli (contatti salvati)"
    )
    assert f.bio_links, "livello assente: i link in bio devono comunque restare"
