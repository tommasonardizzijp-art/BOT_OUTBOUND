"""Adversarial G -- rate-limit e idempotenza (docs/superpowers/qa/2026-08-15-qa-wa-discover-lancio-adversarial.md, casi 38, 40).

Caso 39 NON qui, non serve uno script: verificato con un semplice grep
(`grep -rn Idempotency app/`) che NON esiste nessun layer di
Idempotency-Key in questo backend. Documentato com'e', non simulato: la
difesa reale contro un doppio invio e' l'indice unico parziale
(uq_wa_discover_running_per_number, Task 1) -- gia' verificato piu' volte
in questa sessione (A.1, F.33).

Uso:
    cd "D:\\BOT OUTBOUND\\backend"
    .\\venv\\Scripts\\python.exe scripts/qa_wa_discover/adv_g_rate_limit.py
"""
import asyncio

import _bootstrap  # noqa: E402


async def _caso38_job_id_per_run(maker) -> tuple[bool, str]:
    """38. _job_id e' legato al run_id, non al number_id: due scansioni
    (successive) sullo stesso numero non collidono su ARQ -- accodate
    DISTINTAMENTE. Redis VERO (db 14 isolato), non mockato: enqueue_job
    reale su una coda ARQ vera."""
    import arq

    from app.services import wa_discover_runs
    from app.services.work_enqueue import arq_redis_settings
    from app.workers.wa_discover_worker import enqueue_wa_discover, wa_discover_job_id
    from tests.factories_wa import make_number, make_tenant

    async with maker() as db:
        tenant = await make_tenant(db)
        number = await make_number(db, tenant)
        run_1 = await wa_discover_runs.apri_run(db, tenant_id=tenant.id, number_id=number.id)
        await db.commit()
        run_1_id, number_id = run_1.id, number.id

    # Un secondo run_id sullo STESSO numero (in produzione il primo andrebbe
    # chiuso prima, ma qui interessa solo la non-collisione del job_id).
    # UUID fresco a ogni esecuzione dello script, NON una stringa fissa: con
    # un id fisso il secondo run dello script ripeteva lo STESSO _job_id di
    # un run precedente ancora presente nello stato interno di ARQ (oltre al
    # sorted set 'arq:queue' che questo script ripulisce, ARQ tiene una
    # chiave 'arq:job:<id>' che la pulizia sotto non toccava) -- un FAIL
    # auto-inflitto dello script, non un difetto del prodotto: la garanzia
    # vera (due UUID4 distinti non collidono mai) resta intatta.
    import uuid as _uuid
    run_2_id = f"run-fittizio-adv-g38-{_uuid.uuid4().hex}"

    accodato_1 = await enqueue_wa_discover(number_id, run_1_id)
    accodato_2 = await enqueue_wa_discover(number_id, run_2_id)

    # Pulizia: rimuove i job accodati dalla coda di test, cosi' lo script e'
    # ripetibile senza lasciare job fantasma nel Redis di test.
    redis = await arq.create_pool(arq_redis_settings())
    try:
        for run_id in (run_1_id, run_2_id):
            job_id = wa_discover_job_id(run_id)
            for chiave in (f"arq:job:{job_id}", f"arq:in-progress:{job_id}",
                          f"arq:result:{job_id}"):
                try:
                    await redis.delete(chiave)
                except Exception:  # noqa: BLE001 -- pulizia best-effort
                    pass
            try:
                await redis.zrem("arq:queue", job_id)
            except Exception:  # noqa: BLE001
                pass
    finally:
        await redis.aclose()

    if not (accodato_1 and accodato_2):
        return False, (f"due run diverse sullo stesso numero non accodate entrambe: "
                       f"run_1={accodato_1}, run_2={accodato_2}")
    if wa_discover_job_id(run_1_id) == wa_discover_job_id(run_2_id):
        return False, "i due job_id sono identici (collisione)"
    return True, (f"due enqueue_wa_discover REALI (Redis db 14) sullo stesso "
                 f"number_id, run_id diversi -> entrambi accettati (True, True), "
                 f"job_id distinti ({wa_discover_job_id(run_1_id)} != "
                 f"{wa_discover_job_id(run_2_id)})")


async def _caso40_gate_globale_su_20_numeri(maker) -> tuple[bool, str]:
    """40. Il gate browser_occupato e' GLOBALE sulla macchina (un solo
    browser alla volta), non un contatore di run: con un lock REALE preso
    su Redis (com'e' preso in produzione da un worker che ha aperto un
    browser vero), 20 numeri DIVERSI, tutti 'active', vengono rifiutati
    TUTTI con browser_occupato mentre il lock e' tenuto -- nessuno slitta
    a un codice diverso, nessuno passa.

    Non spawna un vero worker/browser (vietato: scan live in corso): il
    lock e' preso direttamente con wa_profile_lock.held(), lo stesso
    meccanismo che userebbe un worker vero, senza aprire nessun Chromium.
    """
    from app.services import wa_discover_gate, wa_profile_lock
    from tests.factories_wa import make_number, make_tenant
    from unittest.mock import patch

    async def _asy(v):
        async def _f(*a, **kw):
            return v
        return _f

    async with maker() as db:
        tenant = await make_tenant(db)
        numeri = []
        for i in range(20):
            n = await make_number(db, tenant, label=f"Numero G40 {i}")
            numeri.append(n)
        await db.commit()
        numeri_ids = [n.id for n in numeri]

    numero_col_lock = "numero-fittizio-che-tiene-il-browser"
    rifiuti = {}
    with patch.object(wa_discover_gate.bot_state_service, "is_wa_halted", await _asy(False)), \
         patch.object(wa_discover_gate, "ram_libera_mb", lambda: 4000):
        async with wa_profile_lock.held(numero_col_lock):
            async with maker() as db:
                for n in numeri:
                    codice = await wa_discover_gate.puo_lanciare(db, n)
                    rifiuti[n.id] = codice

    non_occupato = {nid: c for nid, c in rifiuti.items() if c != "browser_occupato"}
    if non_occupato:
        return False, (f"con un lock reale tenuto, {len(non_occupato)}/20 numeri NON "
                       f"sono stati rifiutati con browser_occupato: {non_occupato}")
    return True, (f"lock Redis reale tenuto da un numero esterno -> tutti i 20 "
                 f"numeri 'active' diversi rifiutati con 'browser_occupato' "
                 f"(nessuno slittato a un altro codice, nessuno passato)")


async def main() -> None:
    await _bootstrap.crea_schema_pulito()

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import settings
    from app.utils.db_dialect import to_async_database_url

    eng = create_async_engine(to_async_database_url(settings.database_url))
    maker = async_sessionmaker(eng, expire_on_commit=False)

    esiti = {}
    esiti["G.38"] = await _caso38_job_id_per_run(maker)
    esiti["G.40"] = await _caso40_gate_globale_su_20_numeri(maker)

    await eng.dispose()

    tutti_ok = True
    for nome, (ok, dettaglio) in esiti.items():
        print(f"\n=== {'PASS' if ok else 'FAIL'} -- {nome} ===\n{dettaglio}")
        tutti_ok = tutti_ok and ok

    if not tutti_ok:
        raise SystemExit(1)


asyncio.run(main())
