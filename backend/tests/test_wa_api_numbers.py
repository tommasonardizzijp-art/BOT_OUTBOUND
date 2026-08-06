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
    # l'avanzamento automatico riprende comunque: +10/giorno, clampato
    # all'ultimo gradino di wa_warmup_steps default (7 voci): 5+10 -> 7
    assert n.warmup_day == 7
    assert n.warmup_advanced_date == wnm._utc_today_str()


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
