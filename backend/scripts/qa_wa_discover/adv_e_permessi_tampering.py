"""Adversarial E -- permessi e tampering (docs/superpowers/qa/2026-08-15-qa-wa-discover-lancio-adversarial.md, casi 29, 31, 32).

Caso 30 NON qui: "JWT valido ma senza permessi sul tenant del numero" non e'
applicabile. Verificato (app/models/user.py, app/utils/auth_deps.py): User
non ha un tenant_id, un solo ruolo globale admin/operator -- non esiste
concettualmente "il tenant di chi chiede" da poter negare. La barriera
IDOR reale e' un'altra ed e' quella verificata qui: il tenant_id si
risolve SEMPRE dal numero lato server, mai da un campo che il client
potrebbe inviare (caso 31).

Uso:
    cd "D:\\BOT OUTBOUND\\backend"
    .\\venv\\Scripts\\python.exe scripts/qa_wa_discover/adv_e_permessi_tampering.py
"""
import asyncio
from datetime import datetime
from unittest.mock import patch

import _bootstrap  # noqa: E402


async def _caso29_senza_token() -> tuple[bool, str]:
    """29. Chiamata diretta SENZA autenticazione -> 401/403, mai l'esecuzione
    dello scan. Nessun override di get_current_user qui: app REALE, JWT
    attivo per davvero (jwt_secret e' obbligatorio in Settings, validato,
    non e' il 'legacy mode' del commento in main.py -- quel commento e'
    superato dal validator di app/config.py che rifiuta un jwt_secret
    vuoto/corto)."""
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/wa/numbers/00000000-0000-0000-0000-000000000000/discover")
    if r.status_code not in (401, 403):
        return False, f"atteso 401/403 senza token, ricevuto {r.status_code}: {r.text[:300]}"
    return True, f"POST senza Authorization header -> {r.status_code}, nessuno scan avviato"


async def _caso31_e_32(maker) -> tuple[bool, str]:
    """31. tenant_id nel body ignorato, sempre risolto dal numero lato
    server. 32. chiudi_run/apri_run direttamente con un number_id
    inesistente -> nessuna eccezione non gestita."""
    from httpx import ASGITransport, AsyncClient

    from app.database import get_db
    from app.main import app
    from app.models.user import User
    from app.services import wa_discover_runs
    from app.utils.auth_deps import get_current_user
    from tests.factories_wa import make_number, make_tenant

    problemi = []

    async with maker() as db:
        tenant_vero = await make_tenant(db, name="Tenant Vero")
        tenant_estraneo = await make_tenant(db, name="Tenant Estraneo (tampering)")
        number = await make_number(db, tenant_vero)
        await db.commit()
        number_id, tenant_vero_id, tenant_estraneo_id = number.id, tenant_vero.id, tenant_estraneo.id

    def _admin() -> User:
        return User(id="00000000-0000-0000-0000-0000000000e1",
                   email="admin-adv-e@test.local", password_hash="x",
                   role="admin", is_active=True, created_at=datetime.utcnow())

    async def _get_db():
        async with maker() as s:
            yield s

    async def _gate_verde(db, number):
        return None

    async def _enqueue_ok(number_id, run_id):
        return True

    from app.api import wa_numbers

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = _admin
    try:
        with patch.object(wa_numbers.wa_discover_gate, "puo_lanciare", _gate_verde), \
             patch.object(wa_numbers, "enqueue_wa_discover", _enqueue_ok):
            async with AsyncClient(transport=ASGITransport(app=app),
                                   base_url="http://test") as c:
                # 31: tampering. L'endpoint non dichiara un Body param, quindi
                # anche se lo accettasse fisicamente, tenant_id non ha dove
                # legarsi -- il punto e' verificare che passarlo non cambi
                # NULLA nel risultato: la run apre col tenant VERO del
                # numero, non con quello iniettato.
                r = await c.post(f"/api/wa/numbers/{number_id}/discover",
                                 json={"tenant_id": tenant_estraneo_id})
        if r.status_code != 200:
            problemi.append(f"31: POST con tenant_id iniettato nel body ha "
                            f"risposto {r.status_code} invece di 200: {r.text[:300]}")
        else:
            run_id = r.json()["run_id"]
            async with maker() as db:
                run = await wa_discover_runs.ultima_run(db, number_id)
            if run is None or run.tenant_id != tenant_vero_id:
                problemi.append(
                    f"31: BARRIERA IDOR ROTTA -- tenant_id della run e' "
                    f"{run.tenant_id if run else None!r}, atteso il tenant VERO "
                    f"del numero ({tenant_vero_id!r}), non quello iniettato "
                    f"({tenant_estraneo_id!r})")
    finally:
        app.dependency_overrides.clear()

    # 32: chiamata diretta al servizio, bypassando l'endpoint, con un
    # number_id/tenant_id inesistenti -- come farebbe uno script interno
    # buggato. Nessuna eccezione NON gestita attesa (un IntegrityError per
    # violazione di FK e' un esito DEFINITO, non un crash muto).
    async with maker() as db:
        inesistente = "00000000-0000-0000-0000-0000000000ee"
        esito_32 = None
        try:
            await wa_discover_runs.apri_run(db, tenant_id=inesistente,
                                            number_id=inesistente)
            await db.commit()
            esito_32 = "apri_run non ha sollevato (nessun vincolo FK attivo su questo schema)"
        except Exception as exc:  # noqa: BLE001 -- e' proprio quello che osserviamo
            await db.rollback()
            esito_32 = f"apri_run ha sollevato {type(exc).__name__} (IntegrityError attesa su FK mancante)"
        # chiudi_run su un run_id che non esiste mai: CAS, 0 righe toccate,
        # nessuna eccezione per costruzione (UPDATE...WHERE che non trova
        # nulla non e' un errore in SQL).
        try:
            await wa_discover_runs.chiudi_run(db, inesistente, {}, errore="test")
            await db.commit()
        except Exception as exc:  # noqa: BLE001
            problemi.append(f"32: chiudi_run su un run_id inesistente ha sollevato "
                            f"invece di essere un no-op silenzioso ({type(exc).__name__}: {exc})")

    print(f"    [nota 32] {esito_32}")

    if problemi:
        return False, "\n".join(problemi)
    return True, ("31: tenant_id iniettato nel body IGNORATO, run aperta col tenant "
                 "vero risolto dal numero. 32: nessuna eccezione non gestita da "
                 "apri_run/chiudi_run con id inesistenti (vedi nota sopra per il "
                 "comportamento esatto osservato).")


async def main() -> None:
    await _bootstrap.crea_schema_pulito()

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import settings
    from app.utils.db_dialect import to_async_database_url

    eng = create_async_engine(to_async_database_url(settings.database_url))
    maker = async_sessionmaker(eng, expire_on_commit=False)

    ok29, det29 = await _caso29_senza_token()
    print(f"\n=== {'PASS' if ok29 else 'FAIL'} -- E.29 nessun token ===\n{det29}")

    ok3132, det3132 = await _caso31_e_32(maker)
    print(f"\n=== {'PASS' if ok3132 else 'FAIL'} -- E.31+32 tampering tenant_id / id inesistenti ===\n{det3132}")

    await eng.dispose()

    if not (ok29 and ok3132):
        raise SystemExit(1)


asyncio.run(main())
