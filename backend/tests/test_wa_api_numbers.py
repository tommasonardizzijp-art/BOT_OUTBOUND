import pytest

from app.api import wa_numbers
from app.models.wa import WaNumberStatus
from tests.factories_wa import make_number, make_tenant


@pytest.mark.asyncio
async def test_riattivazione_porta_a_pending_qr_non_ad_active(db_session):
    tenant = await make_tenant(db_session)
    n = await make_number(db_session, tenant, status=WaNumberStatus.retired)
    n.sent_today, n.sent_date, n.warmup_day = 57, "2026-07-01", 7
    await db_session.commit()

    await wa_numbers.riattiva(n.id, motivo="numero rientrato dal cliente", db=db_session)
    await db_session.refresh(n)
    assert n.status == WaNumberStatus.pending_qr
    assert n.sent_today == 0 and n.sent_date is None and n.warmup_day == 1
    assert "numero rientrato dal cliente" in (n.notes or "")


@pytest.mark.asyncio
async def test_riattivazione_senza_motivo_rifiutata(db_session):
    tenant = await make_tenant(db_session)
    n = await make_number(db_session, tenant, status=WaNumberStatus.suspended)
    await db_session.commit()
    with pytest.raises(Exception):
        await wa_numbers.riattiva(n.id, motivo="   ", db=db_session)


@pytest.mark.asyncio
async def test_riattivazione_su_numero_attivo_e_un_errore_non_un_no_op(db_session):
    tenant = await make_tenant(db_session)
    n = await make_number(db_session, tenant, status=WaNumberStatus.active)
    await db_session.commit()
    with pytest.raises(Exception):
        await wa_numbers.riattiva(n.id, motivo="tanto per", db=db_session)


@pytest.mark.asyncio
async def test_il_numero_non_e_mai_esposto_in_chiaro(db_session):
    tenant = await make_tenant(db_session)
    await make_number(db_session, tenant, e164="+393421460077")
    await db_session.commit()
    # Filtrato sul PROPRIO tenant: senza, lista() serializza ogni WaNumber del
    # DB sqlite condiviso, e altri file di test (test_wa_number_manager,
    # test_wa_optout, test_wa_worker) inseriscono righe con encrypted_phone
    # finto ("e", "e1"), che fa esplodere decrypt() con InvalidToken. Il
    # fallimento dipendeva dall'ordine dei file, quindi appariva e spariva.
    elenco = await wa_numbers.lista(tenant_id=tenant.id, db=db_session)
    testo = str(elenco)
    assert "3421460077" not in testo
    assert "•" in testo


@pytest.mark.asyncio
async def test_patch_non_puo_scrivere_i_contatori_di_runtime(db_session):
    """Contratto §4.1: sent_today/sent_date/warmup_day sono di M3 in
    scrittura (tranne l'azzeramento in riattivazione). Un PATCH che li
    accetta e' una violazione del contratto, non una comodita'."""
    tenant = await make_tenant(db_session)
    n = await make_number(db_session, tenant)
    n.sent_today = 5
    await db_session.commit()
    await wa_numbers.aggiorna(n.id, {"label": "nuovo nome", "sent_today": 0},
                              db=db_session)
    await db_session.refresh(n)
    assert n.label == "nuovo nome"
    assert n.sent_today == 5      # ignorato, non applicato


@pytest.mark.asyncio
async def test_crea_con_numero_malformato_non_stampa_il_numero_in_chiaro(db_session):
    """Trovato in review: crea() faceva raise HTTPException(422, str(exc)),
    e PhoneNormalizationError porta il numero grezzo nel proprio messaggio
    (stesso rischio di wa_ingest, contratto §2.3)."""
    tenant = await make_tenant(db_session)
    with pytest.raises(Exception) as exc:
        await wa_numbers.crea(
            {"tenant_id": tenant.id, "label": "N", "numero": "ABC123NONVALIDO456"},
            db=db_session)
    assert "ABC123NONVALIDO456" not in str(exc.value)


@pytest.mark.asyncio
async def test_patch_warmup_day_override_manuale_accettato_e_persistito(db_session):
    tenant = await make_tenant(db_session)
    n = await make_number(db_session, tenant)
    n.warmup_day = 1
    await db_session.commit()

    risposta = await wa_numbers.aggiorna(n.id, {"warmup_day": 5}, db=db_session)
    await db_session.refresh(n)
    assert n.warmup_day == 5
    assert risposta["warmup_day"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("valore", [-1, -100])
async def test_patch_warmup_day_negativo_rifiutato(db_session, valore):
    from fastapi import HTTPException

    tenant = await make_tenant(db_session)
    n = await make_number(db_session, tenant)
    n.warmup_day = 3
    await db_session.commit()

    with pytest.raises(HTTPException) as exc:
        await wa_numbers.aggiorna(n.id, {"warmup_day": valore}, db=db_session)
    assert exc.value.status_code == 422

    await db_session.refresh(n)
    assert n.warmup_day == 3  # invariato: la scrittura non e' passata


@pytest.mark.asyncio
@pytest.mark.parametrize("valore", [3.5, "5", None, True])
async def test_patch_warmup_day_non_intero_rifiutato(db_session, valore):
    from fastapi import HTTPException

    tenant = await make_tenant(db_session)
    n = await make_number(db_session, tenant)
    n.warmup_day = 3
    await db_session.commit()

    with pytest.raises(HTTPException) as exc:
        await wa_numbers.aggiorna(n.id, {"warmup_day": valore}, db=db_session)
    assert exc.value.status_code == 422

    await db_session.refresh(n)
    assert n.warmup_day == 3


@pytest.mark.asyncio
async def test_patch_warmup_day_override_non_impedisce_avanzamento_automatico_di_domani(
        db_session):
    """Un override oggi non tocca warmup_advanced_date: il prossimo giro di
    advance_wa_warmup_if_needed (di fatto 'domani') avanza comunque il
    numero, indipendentemente dall'override -- nessuna interazione speciale
    fra i due meccanismi (per design)."""
    from app.services import wa_number_manager as wnm

    tenant = await make_tenant(db_session)
    n = await make_number(db_session, tenant)
    n.warmup_day, n.warmup_advanced_date = 1, None
    await db_session.commit()

    await wa_numbers.aggiorna(n.id, {"warmup_day": 5}, db=db_session)
    await db_session.refresh(n)
    assert n.warmup_day == 5
    assert n.warmup_advanced_date is None  # l'override NON setta la guardia

    await wnm.advance_wa_warmup_if_needed()
    await db_session.refresh(n)
    # L'avanzamento automatico riprende comunque, dal valore impostato a mano:
    # 5 -> 6, un gradino. Cio' che questo test verifica e' che l'override non
    # BLOCCHI l'avanzamento, non di quanto avanzi -- l'entita' del passo e'
    # coperta in test_wa_number_manager (rampa in messaggi/giorno).
    assert n.warmup_day == 6
    assert n.warmup_advanced_date == wnm._utc_today_str()

    # Il vero punto per l'operatore: l'override NON e' una frenata durevole.
    # Chi abbassa warmup_day dopo un warning se lo vede risalire al prossimo
    # avanzamento. La leva che regge nel tempo e' daily_cap.
    assert n.warmup_day > 5


def _lock_occupato(monkeypatch):
    """Doppio del lucchetto profilo sempre occupato."""
    from app.services import wa_profile_lock

    class _Occupato:
        def __call__(self, number_id, ttl_min=None):
            self._number_id = number_id
            return self

        async def __aenter__(self):
            raise wa_profile_lock.WaProfileBusy(self._number_id)

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(wa_numbers.wa_profile_lock, "held", _Occupato())


def _lock_libero(monkeypatch):
    import contextlib

    @contextlib.asynccontextmanager
    async def _libero(number_id, *, ttl_min=None):
        yield "token-di-test"
    monkeypatch.setattr(wa_numbers.wa_profile_lock, "held", _libero)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["login", "check"])
async def test_endpoint_browser_rifiuta_con_409_se_il_profilo_e_occupato(
        db_session, monkeypatch, endpoint):
    """Scenario reale: l'health-check tiene il profilo e l'operatore clicca
    "ri-associa" nella UI. Senza lucchetto partivano due Chromium sullo stesso
    profilo -- il danno che il lucchetto esiste per prevenire. L'endpoint deve
    rifiutare con 409, non aprire un browser."""
    from fastapi import HTTPException
    from app.services import wa_session

    tenant = await make_tenant(db_session)
    n = await make_number(db_session, tenant, status=WaNumberStatus.pending_qr)
    await db_session.commit()

    _lock_occupato(monkeypatch)

    async def _mai(number_id):
        raise AssertionError("il browser non deve essere aperto con il profilo occupato")
    monkeypatch.setattr(wa_session, "assisted_login", _mai)
    monkeypatch.setattr(wa_session, "check_session", _mai)

    with pytest.raises(HTTPException) as exc:
        await getattr(wa_numbers, endpoint)(n.id, db=db_session)
    assert exc.value.status_code == 409
    assert "riprova" in exc.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint,fake_name", [("login", "assisted_login"),
                                                 ("check", "check_session")])
async def test_endpoint_browser_procede_se_il_profilo_e_libero(
        db_session, monkeypatch, endpoint, fake_name):
    """Non-regressione: il lucchetto non deve bloccare il caso normale."""
    from app.services import wa_session

    tenant = await make_tenant(db_session)
    n = await make_number(db_session, tenant, status=WaNumberStatus.pending_qr)
    await db_session.commit()

    _lock_libero(monkeypatch)

    async def _ok(number_id):
        return WaNumberStatus.active
    monkeypatch.setattr(wa_session, fake_name, _ok)

    risposta = await getattr(wa_numbers, endpoint)(n.id, db=db_session)
    assert risposta["status"] == WaNumberStatus.active.value


@pytest.mark.asyncio
@pytest.mark.parametrize("valore", ["molti", 3.5, None, True, [20], {"v": 20}])
async def test_patch_daily_cap_non_intero_rifiutato(db_session, valore):
    """daily_cap NON e' un campo qualsiasi: insieme a warmup_day compone il
    tetto di invio in effective_wa_daily_cap, che fa un `min()` fra i due. Un
    valore non-intero qui non falliva al PATCH (200 OK, scritto a DB) ma piu'
    tardi DENTRO il worker, con un TypeError sul confronto int/str -- e a quel
    punto il numero smette di mandare e la causa e' lontana dal punto in cui
    e' stata scritta. Trovato nel collaudo M5 con `PATCH {"daily_cap":
    "molti"}`; daily_cap era gia' modificabile da prima di M5, la validazione
    mancava per entrambi i campi.
    """
    from fastapi import HTTPException

    tenant = await make_tenant(db_session)
    n = await make_number(db_session, tenant)
    n.daily_cap = 20
    await db_session.commit()

    with pytest.raises(HTTPException) as exc:
        await wa_numbers.aggiorna(n.id, {"daily_cap": valore}, db=db_session)
    assert exc.value.status_code == 422

    await db_session.refresh(n)
    assert n.daily_cap == 20  # invariato: la scrittura non e' passata


@pytest.mark.asyncio
async def test_patch_daily_cap_stringa_non_lascia_il_cap_incalcolabile(db_session):
    """La verifica che conta davvero del test sopra: dopo un PATCH rifiutato,
    il cap effettivo deve restare CALCOLABILE. Senza la validazione questo
    solleva TypeError invece di restituire un numero."""
    from fastapi import HTTPException
    from app.services.wa_number_manager import effective_wa_daily_cap

    tenant = await make_tenant(db_session)
    n = await make_number(db_session, tenant)
    n.daily_cap, n.warmup_day = 20, 1
    await db_session.commit()

    with pytest.raises(HTTPException):
        await wa_numbers.aggiorna(n.id, {"daily_cap": "molti"}, db=db_session)
    await db_session.refresh(n)

    campagna = type("C", (), {"daily_limit": None})()
    assert isinstance(effective_wa_daily_cap(n, campagna), int)


@pytest.mark.asyncio
async def test_patch_warmup_day_oltre_l_ultimo_gradino_rifiutato(db_session, monkeypatch):
    """Sopra l'ultimo gradino configurato warmup_day non ha piu' significato:
    get_wa_warmup_cap clampa comunque all'ultimo valore E il numero esce dalla
    query di avanzamento (`warmup_day < len(steps)`), restando congelato al cap
    MASSIMO per sempre. Accettare 999999 significa quindi offrire un "sblocca
    tutto e non gestirlo piu'" che nessuna schermata dichiara.

    Secondo motivo, invisibile alla suite: la colonna e' Integer, cioe' int4 su
    Postgres. Un valore oltre i 2^31 passa la validazione Python e poi esplode
    al commit con un DataError non catturato -> 500 invece di 422. Su SQLite
    passerebbe in silenzio (collaudo M5: `warmup_day: 10**30` -> 500).
    """
    from fastapi import HTTPException
    from app.config import settings

    monkeypatch.setattr(settings, "wa_warmup_steps", "20,20,30,40,60,80,100")  # 7 gradini
    tenant = await make_tenant(db_session)
    n = await make_number(db_session, tenant)
    n.warmup_day = 3
    await db_session.commit()

    for valore in (8, 999999, 2**31, 10**30):
        with pytest.raises(HTTPException) as exc:
            await wa_numbers.aggiorna(n.id, {"warmup_day": valore}, db=db_session)
        assert exc.value.status_code == 422, f"valore {valore} non rifiutato"

    await db_session.refresh(n)
    assert n.warmup_day == 3

    # L'ultimo gradino esatto resta invece legittimo (plateau raggiunto a mano).
    await wa_numbers.aggiorna(n.id, {"warmup_day": 7}, db=db_session)
    await db_session.refresh(n)
    assert n.warmup_day == 7


@pytest.mark.asyncio
async def test_serializza_espone_il_cap_in_messaggi_non_solo_l_indice(
        db_session, monkeypatch):
    """warmup_day da solo e' un indice: "3" non dice a chi guarda la pagina
    quanti messaggi sono. warmup_cap traduce l'indice in messaggi/giorno, ed e'
    None quando la rampa non pone alcun tetto -- mostrare un numero in quel
    caso suggerirebbe il contrario di cio' che sta succedendo."""
    from app.config import settings

    monkeypatch.setattr(settings, "wa_warmup_steps", "20,20,30,40,60,80,100")
    tenant = await make_tenant(db_session)
    n = await make_number(db_session, tenant)

    n.warmup_day = 4  # 4o gradino = 40 msg/giorno
    await db_session.commit()
    assert wa_numbers._serializza(n)["warmup_cap"] == 40

    n.warmup_day = 0  # fuori warmup: NESSUN tetto di rampa
    await db_session.commit()
    assert wa_numbers._serializza(n)["warmup_cap"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("valore", ["molti", 3.5, True, [20], {"v": 20}, -5, 10**30])
async def test_crea_con_daily_cap_sporco_rifiutato(db_session, valore):
    """Il gemello del test sul PATCH, e conta di PIU': la pagina Numeri non ha
    una form di creazione (waApi.numeri.create non e' chiamato da nessuna UI),
    quindi un numero nasce SOLO da questo endpoint, via API o script. Un
    daily_cap sporco scritto alla nascita non fallisce qui: fallisce piu' tardi
    dentro il worker (effective_wa_daily_cap fa un min() fra i due campi del
    cap) e il numero smette di mandare, con l'errore lontano dalla causa.

    Trovato nella review finale di M5: la validazione era stata messa solo su
    aggiorna(), e le batterie adversarial avevano fuzzato solo il PATCH --
    questa meta' dell'ingresso era rimasta scoperta da entrambi.
    """
    from fastapi import HTTPException

    tenant = await make_tenant(db_session)
    with pytest.raises(HTTPException) as exc:
        await wa_numbers.crea(
            {"tenant_id": tenant.id, "label": "N", "numero": "+393421460099",
             "daily_cap": valore},
            db=db_session)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_crea_senza_daily_cap_usa_il_default(db_session):
    """La validazione non deve rompere il caso normale: daily_cap assente
    resta legittimo e prende il default di config."""
    from app.config import settings

    tenant = await make_tenant(db_session)
    risposta = await wa_numbers.crea(
        {"tenant_id": tenant.id, "label": "N-default", "numero": "+393421460098"},
        db=db_session)
    assert risposta["daily_cap"] == settings.wa_daily_cap_default
