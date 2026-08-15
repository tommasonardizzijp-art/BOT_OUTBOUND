"""Adversarial A.12 -- lock Redis REALE preso su un ALTRO numero, poi il
gate chiamato sul numero A.

docs/superpowers/qa/2026-08-15-qa-wa-discover-lancio-adversarial.md, caso 12:
"Lock del profilo presente ma di un ALTRO numero (prendere il lock Redis
wa:profile-lock:* su un numero B, poi POST sul numero A) -> rifiutato
browser_occupato anche per A: il gate e' GLOBALE sulla macchina, non
per-numero -- verificare che non venga invece rifiutato con un codice
diverso o, peggio, lasciato passare."

Il test unit esistente (test_wa_discover_gate.py::
test_browser_occupato_da_UN_ALTRO_numero) MOCKA profilo_occupato_da per
tornare direttamente "un-altro-numero" -- verifica la logica del gate, non
che un lock Redis VERO su una chiave diversa (wa:profile-lock:<altro-id>)
venga trovato dalla scan_iter reale di profilo_occupato_da. Qui si usa
wa_profile_lock.held() per davvero, su Redis vero (db 14, MAI il db 0 di
produzione -- stesso db isolato usato dalla sonda di connettivita' di
questa sessione).

Uso:
    cd "D:\\BOT OUTBOUND\\backend"
    .\\venv\\Scripts\\python.exe scripts/qa_wa_discover/adv_a12_lock_altro_numero_reale.py
"""
import asyncio
import uuid
from unittest.mock import patch

import _bootstrap  # noqa: E402


def _asy(valore):
    async def _f(*a, **kw):
        return valore
    return _f


async def main() -> None:
    await _bootstrap.crea_schema_pulito()

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import settings
    from app.services import wa_discover_gate, wa_profile_lock
    from app.utils.db_dialect import to_async_database_url
    from tests.factories_wa import make_number, make_tenant

    eng = create_async_engine(to_async_database_url(settings.database_url))
    maker = async_sessionmaker(eng, expire_on_commit=False)
    async with maker() as db:
        tenant = await make_tenant(db)
        numero_a = await make_number(db, tenant, label="Numero A (quello che chiede)")
        await db.commit()
        numero_a_id = numero_a.id

    # number_id B: NON serve una riga WaNumber vera, il lock Redis e' chiavato
    # solo sull'id -- e il gate su A non tocca mai la riga di B.
    numero_b_id = str(uuid.uuid4())

    problemi = []
    with patch.object(wa_discover_gate.bot_state_service, "is_wa_halted", _asy(False)), \
         patch.object(wa_discover_gate, "ram_libera_mb", lambda: 4000):
        # Lock VERO su Redis (db 14), preso sul numero B, ancora tenuto
        # mentre si interroga il gate per A.
        async with wa_profile_lock.held(numero_b_id):
            async with maker() as db:
                codice = await wa_discover_gate.puo_lanciare(db, numero_a)

        # Fuori dal 'held' il lock e' rilasciato: riprova come controllo,
        # deve tornare verde (altrimenti il FAIL sopra potrebbe essere un
        # lock rimasto sporco da un run precedente, non il comportamento
        # sotto esame).
        async with maker() as db:
            codice_dopo_rilascio = await wa_discover_gate.puo_lanciare(db, numero_a)

    if codice != "browser_occupato":
        problemi.append(
            f"lock reale preso su un ALTRO numero (B={numero_b_id}) doveva "
            f"rifiutare A con 'browser_occupato' (gate globale), ricevuto {codice!r}")
    if codice_dopo_rilascio != "ram_insufficiente" and codice_dopo_rilascio is not None:
        # Con is_wa_halted e ram mockati verdi, dopo il rilascio ci si aspetta
        # None (via libera) -- salvo che un'altra run/lock residuo intervenga,
        # nel qual caso lo si segnala com'e' invece di assumere.
        problemi.append(
            f"dopo il rilascio del lock su B, il gate su A ha dato {codice_dopo_rilascio!r} "
            "invece di via libera (None) -- possibile stato sporco pre-esistente")

    await eng.dispose()

    if problemi:
        _bootstrap.esito("A.12 lock reale di un altro numero", False, "\n".join(problemi))
    else:
        _bootstrap.esito(
            "A.12 lock reale di un altro numero", True,
            f"lock Redis vero preso su B ({numero_b_id[:8]}...) -> gate su A "
            "rifiutato 'browser_occupato' (conferma: gate globale sulla macchina, "
            "non per-numero). Dopo il rilascio, via libera.")


asyncio.run(main())
