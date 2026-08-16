"""Adversarial A.11 -- Redis IRRAGGIUNGIBILE per davvero, proprio durante il
controllo browser_occupato del gate.

docs/superpowers/qa/2026-08-15-qa-wa-discover-lancio-adversarial.md, caso 11:
"Redis irraggiungibile PROPRIO durante il gate (fermare Redis di test, mai
quello di produzione, subito prima del controllo browser_occupato) -> 409
browser_occupato (fail-closed, trattato come occupato), MAI un 500."

Il test unit esistente (test_wa_discover_gate.py::
test_redis_irraggiungibile_e_fail_closed_non_500) MOCKA profilo_occupato_da
per sollevare ConnectionError -- verifica lo stesso except, ma non un
fallimento di connessione VERO. Qui invece si fa fallire per davvero: si
punta wa_profile_lock su una porta che rifiuta la connessione (127.0.0.1:1,
mai in ascolto su nessuna macchina), cosi' arq.create_pool tenta una
connessione TCP reale e fallisce con un errore reale -- non un raise
iniettato a mano.

NON tocca il Redis vero (ne' produzione ne' quello locale che il worker
ARQ/lock reali usano in questo momento): la porta 1 non e' mai in ascolto,
non serve fermare nessun servizio.

Uso:
    cd "D:\\BOT OUTBOUND\\backend"
    .\\venv\\Scripts\\python.exe scripts/qa_wa_discover/adv_a11_redis_irraggiungibile_reale.py
"""
import asyncio
import time
from unittest.mock import patch

import _bootstrap  # noqa: E402


async def main() -> None:
    await _bootstrap.crea_schema_pulito()

    from arq.connections import RedisSettings
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import settings
    from app.services import wa_discover_gate
    from app.utils.db_dialect import to_async_database_url
    from tests.factories_wa import make_number, make_tenant

    eng = create_async_engine(to_async_database_url(settings.database_url))
    maker = async_sessionmaker(eng, expire_on_commit=False)
    async with maker() as db:
        tenant = await make_tenant(db)
        number = await make_number(db, tenant)
        await db.commit()

    def _settings_irraggiungibili() -> RedisSettings:
        # Porta 1 (non 6379): connessione rifiutata VERA, non un raise
        # iniettato. conn_retries basso e conn_timeout corto -- lo script
        # deve fallire in fretta, non aspettare i retry di produzione
        # (10 tentativi x 2s che userebbe la config reale).
        return RedisSettings(host="127.0.0.1", port=1, database=0,
                             conn_timeout=2, conn_retries=1, conn_retry_delay=0)

    problemi = []
    with patch.object(wa_discover_gate.bot_state_service, "is_wa_halted",
                      _asy(False)), \
         patch.object(wa_discover_gate.wa_profile_lock, "arq_redis_settings",
                      _settings_irraggiungibili):
        t0 = time.monotonic()
        try:
            async with maker() as db:
                codice = await wa_discover_gate.puo_lanciare(db, number)
        except Exception as exc:  # noqa: BLE001 -- e' proprio quello che NON deve succedere
            problemi.append(
                f"puo_lanciare ha sollevato invece di tornare un codice "
                f"({type(exc).__name__}: {exc}) -- sarebbe un 500 nell'endpoint reale")
            codice = None
        durata_s = time.monotonic() - t0

    if codice is not None and codice != "browser_occupato":
        problemi.append(f"atteso 'browser_occupato' (fail-closed), ricevuto {codice!r}")
    if durata_s > 15:
        problemi.append(f"troppo lento ({durata_s:.1f}s): la config di produzione "
                        "(conn_retries=10, conn_retry_delay=2) terrebbe un utente vero "
                        "in attesa di 409 per ~20s+")

    await eng.dispose()

    if problemi:
        _bootstrap.esito("A.11 Redis irraggiungibile davvero", False, "\n".join(problemi))
    else:
        _bootstrap.esito(
            "A.11 Redis irraggiungibile davvero", True,
            f"connessione TCP reale rifiutata (127.0.0.1:1) durante il gate -> "
            f"'browser_occupato' in {durata_s:.2f}s, nessuna eccezione risalita "
            "(nessun 500).")


def _asy(valore):
    async def _f(*a, **kw):
        return valore
    return _f


asyncio.run(main())
