"""Adversarial F -- macchina a stati (docs/superpowers/qa/2026-08-15-qa-wa-discover-lancio-adversarial.md, casi 33, 34, 35, 37).

Caso 36 NON qui: "kill-switch alzato A META' di uno scan" e' gia' coperto
per davvero da `test_kill_switch_a_meta_giro_si_ferma` in
backend/tests/test_wa_discover_run.py (is_wa_halted diventa vero A META'
lotto -- non prima -- e lo scan si ferma con motivo='wa_halted', che e' in
MOTIVI_NON_GUASTO quindi chiudi_run lo chiude 'done', mai 'running'
appeso). Rieseguito ora, verde.

Uso:
    cd "D:\\BOT OUTBOUND\\backend"
    .\\venv\\Scripts\\python.exe scripts/qa_wa_discover/adv_f_macchina_stati.py
"""
import asyncio
from datetime import datetime
from unittest.mock import patch

import _bootstrap  # noqa: E402


async def _caso33_due_post_sequenziali(app, maker) -> tuple[bool, str]:
    """33. Due POST SEQUENZIALI (non concorrenti): il secondo, dopo che il
    primo ha gia' aperto la run, riceve 409 scan_gia_in_corso in modo
    deterministico. Gate REALE (solo Redis/RAM/kill-switch mockati verdi,
    run_attiva/apri_run/chiudi_se_orfana sono il codice vero)."""
    from httpx import ASGITransport, AsyncClient

    from app.api import wa_numbers
    from app.database import get_db
    from app.utils.auth_deps import get_current_user
    from tests.factories_wa import make_number, make_tenant

    async with maker() as db:
        tenant = await make_tenant(db)
        number = await make_number(db, tenant)
        await db.commit()
        number_id = number.id

    def _admin():
        return __import__("app.models.user", fromlist=["User"]).User(
            id="00000000-0000-0000-0000-0000000000f3", email="admin-adv-f33@test.local",
            password_hash="x", role="admin", is_active=True, created_at=datetime(2026, 1, 1))

    async def _get_db():
        async with maker() as s:
            yield s

    async def _redis_libero(*a, **kw):
        return None

    async def _kill_switch_spento(*a, **kw):
        return False

    async def _enqueue_ok(number_id, run_id):
        return True

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = _admin
    try:
        with patch.object(wa_numbers.wa_discover_gate.bot_state_service,
                          "is_wa_halted", _kill_switch_spento), \
             patch.object(wa_numbers.wa_discover_gate.wa_profile_lock,
                          "profilo_occupato_da", _redis_libero), \
             patch.object(wa_numbers.wa_discover_gate, "ram_libera_mb", lambda: 4000), \
             patch.object(wa_numbers, "enqueue_wa_discover", _enqueue_ok):
            async with AsyncClient(transport=ASGITransport(app=app),
                                   base_url="http://test") as c:
                r1 = await c.post(f"/api/wa/numbers/{number_id}/discover")
                r2 = await c.post(f"/api/wa/numbers/{number_id}/discover")
    finally:
        app.dependency_overrides.clear()

    if r1.status_code != 200:
        return False, f"primo POST atteso 200, ricevuto {r1.status_code}: {r1.text[:300]}"
    if r2.status_code != 409:
        return False, f"secondo POST (sequenziale, dopo il primo) atteso 409, ricevuto {r2.status_code}: {r2.text[:300]}"
    if r2.json().get("detail", {}).get("codice") != "scan_gia_in_corso":
        return False, f"secondo POST: codice atteso 'scan_gia_in_corso', ricevuto {r2.json()}"
    return True, "1o POST -> 200, 2o POST (sequenziale, stesso numero) -> 409 scan_gia_in_corso, deterministico"


async def _caso34_tutti_gli_stati_non_attivi(app, maker) -> tuple[bool, str]:
    """34. POST su un numero in OGNUNO degli stati non-'active' -> sempre
    409 numero_non_attivo. Il test esistente copre solo 'retired'."""
    from httpx import ASGITransport, AsyncClient

    from app.database import get_db
    from app.models.wa import WaNumberStatus
    from app.utils.auth_deps import get_current_user
    from app.workers.wa_discover_worker import enqueue_wa_discover as _vero_enqueue  # noqa: F401
    from app.api import wa_numbers
    from tests.factories_wa import make_number, make_tenant

    async def _get_db():
        async with maker() as s:
            yield s

    def _admin():
        return __import__("app.models.user", fromlist=["User"]).User(
            id="00000000-0000-0000-0000-0000000000f4", email="admin-adv-f34@test.local",
            password_hash="x", role="admin", is_active=True, created_at=datetime(2026, 1, 1))

    async def _enqueue_ok(number_id, run_id):
        return True

    stati_non_attivi = [s for s in WaNumberStatus if s != WaNumberStatus.active]
    problemi = []
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = _admin
    try:
        with patch.object(wa_numbers, "enqueue_wa_discover", _enqueue_ok):
            for stato in stati_non_attivi:
                async with maker() as db:
                    tenant = await make_tenant(db, name=f"Tenant F34 {stato.value}")
                    number = await make_number(db, tenant, status=stato)
                    await db.commit()
                    number_id = number.id
                async with AsyncClient(transport=ASGITransport(app=app),
                                       base_url="http://test") as c:
                    r = await c.post(f"/api/wa/numbers/{number_id}/discover")
                if r.status_code != 409 or r.json().get("detail", {}).get("codice") != "numero_non_attivo":
                    problemi.append(f"stato={stato.value}: atteso 409 numero_non_attivo, "
                                    f"ricevuto {r.status_code} {r.text[:200]}")
    finally:
        app.dependency_overrides.clear()

    if problemi:
        return False, "\n".join(problemi)
    return True, f"tutti gli stati non-active testati ({[s.value for s in stati_non_attivi]}) -> 409 numero_non_attivo"


async def _caso35_idempotenza_end_to_end(maker) -> tuple[bool, str]:
    """35. Due chiamate REALI del worker sulla stessa run (non due chiamate
    dirette a chiudi_run come nel test unit esistente) -- la seconda non
    deve sovrascrivere finished_at ne' i contatori della prima."""
    from sqlalchemy import select
    from app.models.wa import WaDiscoverRun
    from app.services import wa_discover_runs
    from app.workers import wa_discover_worker
    from tests.factories_wa import make_number, make_tenant

    async with maker() as db:
        tenant = await make_tenant(db)
        number = await make_number(db, tenant)
        run = await wa_discover_runs.apri_run(db, tenant_id=tenant.id, number_id=number.id)
        await db.commit()
        run_id, number_id = run.id, number.id

    async def _prima_chiamata(number_id, **kw):
        return {"salvate": 100, "aggiornate": 5, "saltate_gia_note": 3,
               "non_verificate": 0, "dichiarato": 108, "motivo": "completato"}

    async def _seconda_chiamata_diversa(number_id, **kw):
        return {"salvate": 999, "aggiornate": 999, "saltate_gia_note": 999,
               "non_verificate": 999, "dichiarato": 999, "motivo": "raccolta_parziale"}

    with patch.object(wa_discover_worker, "esegui_discover_run", _prima_chiamata):
        await wa_discover_worker.wa_discover_task({}, number_id, run_id)

    async with maker() as db:
        dopo_prima = await db.scalar(select(WaDiscoverRun).where(WaDiscoverRun.id == run_id))
        finished_at_prima, salvate_prima = dopo_prima.finished_at, dopo_prima.salvate

    with patch.object(wa_discover_worker, "esegui_discover_run", _seconda_chiamata_diversa):
        await wa_discover_worker.wa_discover_task({}, number_id, run_id)

    async with maker() as db:
        dopo_seconda = await db.scalar(select(WaDiscoverRun).where(WaDiscoverRun.id == run_id))

    if dopo_seconda.salvate != salvate_prima or dopo_seconda.motivo != "completato":
        return False, (f"la seconda chiamata (idempotente attesa) ha sovrascritto "
                       f"la run: salvate {salvate_prima}->{dopo_seconda.salvate}, "
                       f"motivo->{dopo_seconda.motivo!r}")
    if dopo_seconda.finished_at != finished_at_prima:
        return False, "finished_at cambiato dalla seconda chiamata (attesa idempotenza)"
    return True, (f"due chiamate REALI di wa_discover_task sulla stessa run: la "
                 f"seconda (esito diverso: salvate=999/motivo=raccolta_parziale) "
                 f"e' un no-op, la run resta quella della prima (salvate="
                 f"{salvate_prima}, motivo=completato, finished_at invariato)")


async def _caso37_riattivazione_con_run_aperta(maker) -> tuple[bool, str]:
    """37. Un numero torna da 'retired' a 'pending_qr' MENTRE una run
    (impossibilmente, ma verificarlo) e' ancora 'running' su quel numero --
    nessuna corruzione, comportamento definito. Scenario forzato a mano
    (bypassa il flusso normale, che non lo permetterebbe): verifica che
    leggere lo stato (GET reale) non esploda e riporti fedelmente cio' che
    c'e' a DB."""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import select

    from app.database import get_db
    from app.main import app
    from app.models.wa import WaNumber, WaNumberStatus
    from app.services import wa_discover_runs
    from app.utils.auth_deps import get_current_user
    from tests.factories_wa import make_number, make_tenant

    async with maker() as db:
        tenant = await make_tenant(db)
        number = await make_number(db, tenant, status=WaNumberStatus.retired)
        run = await wa_discover_runs.apri_run(db, tenant_id=tenant.id, number_id=number.id)
        await db.commit()
        number_id, run_id = number.id, run.id

    # Riattivazione forzata a mano (bypassa il flusso QR normale): lo stato
    # che il caso 37 vuole verificare NON dovrebbe accadere per la strada
    # ordinaria (un numero retired con QR normalmente non ha run aperte).
    async with maker() as db:
        n = await db.scalar(select(WaNumber).where(WaNumber.id == number_id))
        n.status = WaNumberStatus.pending_qr
        await db.commit()

    def _admin():
        return __import__("app.models.user", fromlist=["User"]).User(
            id="00000000-0000-0000-0000-0000000000f7", email="admin-adv-f37@test.local",
            password_hash="x", role="admin", is_active=True, created_at=datetime(2026, 1, 1))

    async def _get_db():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = _admin
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/api/wa/numbers/{number_id}/discover")
    except Exception as exc:  # noqa: BLE001
        app.dependency_overrides.clear()
        return False, f"GET sullo stato incoerente ha sollevato invece di rispondere ({type(exc).__name__}: {exc})"
    app.dependency_overrides.clear()

    if r.status_code != 200:
        return False, f"GET atteso 200 (deve solo riportare lo stato, non giudicarlo), ricevuto {r.status_code}: {r.text[:300]}"
    corpo = r.json()
    if corpo["ultima"]["id"] != run_id or corpo["ultima"]["stato"] != "running" or corpo["in_corso"] is not True:
        return False, f"GET non riporta fedelmente lo stato incoerente a DB: {corpo}"
    return True, ("numero riattivato (retired->pending_qr) CON una run ancora "
                 "'running' aperta (stato impossibile per la strada normale, "
                 "forzato a mano qui): GET non esplode, riporta fedelmente "
                 "in_corso=True/stato=running -- nessuna corruzione, nessuna "
                 "eccezione, il numero_non_attivo del gate impedirebbe comunque "
                 "un NUOVO lancio su pending_qr indipendentemente da questo.")


async def main() -> None:
    await _bootstrap.crea_schema_pulito()

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import settings
    from app.main import app
    from app.utils.db_dialect import to_async_database_url

    eng = create_async_engine(to_async_database_url(settings.database_url))
    maker = async_sessionmaker(eng, expire_on_commit=False)

    esiti = {}
    esiti["F.33"] = await _caso33_due_post_sequenziali(app, maker)
    esiti["F.34"] = await _caso34_tutti_gli_stati_non_attivi(app, maker)
    esiti["F.35"] = await _caso35_idempotenza_end_to_end(maker)
    esiti["F.37"] = await _caso37_riattivazione_con_run_aperta(maker)

    await eng.dispose()

    tutti_ok = True
    for nome, (ok, dettaglio) in esiti.items():
        print(f"\n=== {'PASS' if ok else 'FAIL'} -- {nome} ===\n{dettaglio}")
        tutti_ok = tutti_ok and ok

    if not tutti_ok:
        raise SystemExit(1)


asyncio.run(main())
