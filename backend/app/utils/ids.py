"""Validazione di id esterni prima che arrivino a una query.

Estratto in review finale di branch (Fase B WhatsApp): la stessa validazione
era duplicata identica in `wa_promote/promozione.py` e
`wa_promote/arruolamento.py`, entrambe per lo stesso bug trovato in QA di
fine modulo (un null byte in un id fa sollevare ad asyncpg un
CharacterNotInRepertoireError non catturato su `db.get()` -- 500 grezzo
invece di uno scarto gestito). Due copie della stessa correzione rischiano
di divergere silenziosamente: una viene raffinata, l'altra no.
"""
import uuid


def uuid_valido(id_: str) -> bool:
    """Gli id di questo dominio sono sempre uuid4 (`String(36)` nei
    modelli). Un id malformato (null byte, non-uuid, 10k caratteri) non deve
    mai arrivare al driver: si valida qui, prima di ogni `db.get()`."""
    try:
        uuid.UUID(id_)
        return True
    except (ValueError, AttributeError, TypeError):
        return False
