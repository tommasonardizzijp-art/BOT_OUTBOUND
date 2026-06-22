"""Classificazione errori DB/rete transitori.

Un blip di rete verso Supabase (proxy/USB/WiFi caduto, pooler irraggiungibile,
WinError 121 "timeout del semaforo" su Windows) faceva fallire il job ARQ e
fermava la campagna. Questi errori sono transitori: il worker li converte in
``Retry(defer=...)`` cosi' il job riparte da solo quando la rete torna, invece
di morire.

NON classificare qui errori applicativi/SQL (vincoli, programmazione): solo
problemi di connessione/trasporto che ha senso ritentare.
"""
import asyncio

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

# OSError copre WinError 121, ECONNREFUSED/RESET, timeout socket; TimeoutError
# (builtin, py3.11+) e asyncio.TimeoutError ne sono sottoclassi.
_TRANSIENT_TYPES = (
    OSError,
    ConnectionError,
    asyncio.TimeoutError,
    InterfaceError,
    OperationalError,
)


def is_transient_db_error(exc: BaseException) -> bool:
    """True se ``exc`` (o una sua causa nella catena) e' un problema di
    connessione DB/rete che ha senso ritentare."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, _TRANSIENT_TYPES):
            return True
        # Connessione invalidata da SQLAlchemy a runtime.
        if isinstance(cur, DBAPIError) and getattr(cur, "connection_invalidated", False):
            return True
        # Errori di connessione asyncpg (PostgresConnectionError & co.).
        mod = type(cur).__module__ or ""
        if mod.startswith("asyncpg") and "Connection" in type(cur).__name__:
            return True
        # Risali la catena: __cause__ (raise from) o .orig (wrapper DBAPI SQLAlchemy).
        cur = getattr(cur, "__cause__", None) or getattr(cur, "orig", None)
    return False
