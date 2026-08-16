"""Adversarial A.6 -- GET e POST concorrenti sullo stesso numero.

docs/superpowers/qa/2026-08-15-qa-wa-discover-lancio-adversarial.md, caso 6:
"GET e POST concorrenti sullo stesso numero (uno legge lo stato mentre
l'altro avvia) -> il GET non deve mai vedere uno stato a meta' scrittura
(nessuna riga con stato NULL o campi parzialmente popolati)."

asyncio.gather vero fra un client che fa GET e uno che fa POST, ciascuno con
la propria sessione DB indipendente (stesso schema di
test_due_post_concorrenti_uno_solo_vince in test_wa_discover_launch_api.py).
Ripetuto su N numeri diversi (un POST apre una run 'running' che nessuno
richiude in questo script, quindi ogni trial vuole un numero vergine).

Uso:
    cd "D:\\BOT OUTBOUND\\backend"
    .\\venv\\Scripts\\python.exe scripts/qa_wa_discover/adv_a06_get_post_concorrenti.py
"""
import asyncio
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

import _bootstrap  # noqa: E402 -- deve girare prima di import app.*

N_TRIAL = 20


@contextmanager
def _override_dipendenze(app, get_db, get_current_user, session_maker):
    from app.models.user import User

    def _admin() -> User:
        return User(id="00000000-0000-0000-0000-0000000000a6",
                   email="admin-adv-a6@test.local", password_hash="x",
                   role="admin", is_active=True, created_at=datetime.utcnow())

    async def _get_db():
        async with session_maker() as s:
            yield s

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = _admin
    try:
        yield
    finally:
        app.dependency_overrides.clear()


async def main() -> None:
    await _bootstrap.crea_schema_pulito()

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.api import wa_numbers
    from app.config import settings
    from app.database import get_db
    from app.main import app
    from app.utils.auth_deps import get_current_user
    from app.utils.db_dialect import to_async_database_url
    from tests.factories_wa import make_number, make_tenant

    eng = create_async_engine(to_async_database_url(settings.database_url))
    maker = async_sessionmaker(eng, expire_on_commit=False)

    async def _gate_verde(db, number):
        return None

    async def _enqueue_ok(number_id, run_id):
        return True

    anomalie = []
    async with maker() as setup_db:
        tenant = await make_tenant(setup_db)
        await setup_db.commit()

    with patch.object(wa_numbers.wa_discover_gate, "puo_lanciare", _gate_verde), \
         patch.object(wa_numbers, "enqueue_wa_discover", _enqueue_ok), \
         _override_dipendenze(app, get_db, get_current_user, maker):
        for i in range(N_TRIAL):
            async with maker() as setup_db:
                tenant = await make_tenant(setup_db, name=f"Tenant A6 {i}")
                number = await make_number(setup_db, tenant)
                await setup_db.commit()
                number_id = number.id

            async with AsyncClient(transport=ASGITransport(app=app),
                                   base_url="http://test") as c1, \
                       AsyncClient(transport=ASGITransport(app=app),
                                   base_url="http://test") as c2:
                r_get, r_post = await asyncio.gather(
                    c1.get(f"/api/wa/numbers/{number_id}/discover"),
                    c2.post(f"/api/wa/numbers/{number_id}/discover"),
                )

            if r_get.status_code != 200:
                anomalie.append(f"trial {i}: GET status {r_get.status_code} ({r_get.text})")
                continue
            corpo = r_get.json()
            # Contratto della risposta (stato_discover in wa_numbers.py):
            # 'ultima' None O un dict con 'stato' valorizzato in un set noto;
            # 'in_corso' coerente con 'ultima.stato'. Mai un dict con 'stato'
            # None/mancante (scrittura a meta').
            ultima = corpo.get("ultima")
            in_corso = corpo.get("in_corso")
            if ultima is not None:
                stato = ultima.get("stato")
                if stato not in ("running", "done", "failed"):
                    anomalie.append(
                        f"trial {i}: 'ultima.stato' fuori contratto: {stato!r} "
                        f"(corpo: {corpo})")
                if (in_corso is True) != (stato == "running"):
                    anomalie.append(
                        f"trial {i}: 'in_corso' incoerente con stato: "
                        f"in_corso={in_corso} stato={stato!r}")
            elif in_corso is not False:
                anomalie.append(
                    f"trial {i}: 'ultima' None ma 'in_corso' non e' False ({corpo})")

    if anomalie:
        _bootstrap.esito("A.6 GET/POST concorrenti", False, "\n".join(anomalie))
    else:
        _bootstrap.esito(
            "A.6 GET/POST concorrenti", True,
            f"{N_TRIAL} trial, ogni GET concorrente con un POST reale ha "
            "restituito una risposta internamente coerente (mai stato "
            "NULL/fuori contratto, mai in_corso incoerente con lo stato).")

    await eng.dispose()


asyncio.run(main())
