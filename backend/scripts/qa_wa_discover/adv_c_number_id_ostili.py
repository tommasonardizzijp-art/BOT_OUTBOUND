"""Adversarial C -- number_id ostile (docs/superpowers/qa/2026-08-15-qa-wa-discover-lancio-adversarial.md, casi 20, 21, 22, 23).

Casi NON qui:
- 19 (number_id di un altro tenant -> 404 IDOR): NON APPLICABILE cosi' come
  scritto. Verificato leggendo app/models/user.py e app/utils/auth_deps.py:
  questo sistema non ha un tenant_id sulla sessione autenticata (un solo
  User globale admin/operator, nessuno scoping per-tenant sui token). Non
  esiste "il tenant di chi chiede" da confrontare col numero. La barriera
  IDOR reale che il codice dichiara e implementa e' un'altra: il tenant_id
  della run/promozione si risolve SEMPRE dal numero lato server, mai da un
  campo del client -- quella e' il caso 31, verificato per davvero qui sotto
  in adv_e_permessi_tampering.py.
- 24 (motivo non mappato -> UI mostra il codice grezzo): richiede rendering
  frontend, nessun runner di test JS in questo repo (verificato:
  frontend/package.json non ha script "test"). Verificato SOLO staticamente
  dal sorgente: frontend/app/wa/numeri/page.tsx usa
  `MOTIVO_LABEL[motivo] ?? motivo` (fallback esplicito al codice grezzo),
  non e' una esecuzione vera.
- 25 (numero in chiaro nell'errore, end-to-end dal worker vero): script
  separato, adv_c25_masking_worker_reale.py.

Uso:
    cd "D:\\BOT OUTBOUND\\backend"
    .\\venv\\Scripts\\python.exe scripts/qa_wa_discover/adv_c_number_id_ostili.py
"""
import asyncio
from datetime import datetime

import _bootstrap  # noqa: E402


async def main() -> None:
    await _bootstrap.crea_schema_pulito()

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import settings
    from app.database import get_db
    from app.main import app
    from app.models.user import User
    from app.utils.auth_deps import get_current_user
    from app.utils.db_dialect import to_async_database_url

    eng = create_async_engine(to_async_database_url(settings.database_url))
    maker = async_sessionmaker(eng, expire_on_commit=False)

    def _admin() -> User:
        return User(id="00000000-0000-0000-0000-0000000000c0",
                   email="admin-adv-c@test.local", password_hash="x",
                   role="admin", is_active=True, created_at=datetime.utcnow())

    async def _get_db():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = _admin

    problemi = []
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            # 20. number_id malformato (non UUID)
            r = await c.get("/api/wa/numbers/abc/discover")
            if r.status_code != 404:
                problemi.append(f"20 (malformato 'abc'): atteso 404, ricevuto "
                                f"{r.status_code}: {r.text[:300]}")

            # 21. number_id vuoto -- path-param assente (route non matcha
            # affatto: verificato che sia un 404 di ROUTING, non un errore
            # su un handler che riceve number_id='')
            r = await c.get("/api/wa/numbers//discover")
            if r.status_code not in (404, 422):
                problemi.append(f"21 (vuoto): atteso 404/422 di routing, ricevuto "
                                f"{r.status_code}: {r.text[:300]}")

            # 22. number_id da 10.000 caratteri
            enorme = "a" * 10_000
            try:
                r = await c.get(f"/api/wa/numbers/{enorme}/discover", timeout=15.0)
                if r.status_code not in (404, 414, 422):
                    problemi.append(f"22 (10k char): atteso 404/414/422, ricevuto "
                                    f"{r.status_code}: {r.text[:300]}")
            except Exception as exc:  # noqa: BLE001
                problemi.append(f"22 (10k char): la richiesta stessa e' fallita "
                                f"invece di tornare una risposta gestita "
                                f"({type(exc).__name__}: {exc})")

            # 23. number_id con un null byte incorporato
            try:
                r = await c.get("/api/wa/numbers/abc%00xyz/discover")
                if r.status_code not in (400, 404, 422):
                    problemi.append(f"23 (null byte): atteso 400/404/422, ricevuto "
                                    f"{r.status_code}: {r.text[:300]}")
                elif "\x00" in r.text:
                    problemi.append("23 (null byte): il null byte torna nel corpo "
                                    "della risposta senza sanificazione")
            except Exception as exc:  # noqa: BLE001
                problemi.append(f"23 (null byte): la richiesta stessa e' fallita "
                                f"({type(exc).__name__}: {exc})")
    finally:
        app.dependency_overrides.clear()
        await eng.dispose()

    if problemi:
        _bootstrap.esito("C number_id ostile (20,21,22,23)", False, "\n".join(problemi))
    else:
        _bootstrap.esito(
            "C number_id ostile (20,21,22,23)", True,
            "malformato -> 404; vuoto -> 404/422 di routing; 10k caratteri -> "
            "gestito senza hang ne' 500; null byte -> gestito, mai riflesso nel "
            "corpo della risposta.")


asyncio.run(main())
