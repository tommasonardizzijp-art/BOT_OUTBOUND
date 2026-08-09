"""Targa provvisoria per i contatti raccolti dal browser.

Il canale browser non conosce il pk Instagram: dalla pagina del thread si ricava
lo username, non il numero (misurato — l'unico numero lungo accanto allo username
e' un segnaposto costante). Ma `ig_user_id` non e' un campo qualunque: e' sotto
UniqueConstraint(campaign_id, ig_user_id) ed e' la chiave di prenotazione
cross-account che impedisce a due account di scrivere alla stessa persona.

Quindi si assegna una targa PROVVISORIA, sostituita con quella vera durante
l'arricchimento (che naviga per username e riporta il pk).

NEGATIVA per costruzione: Instagram non assegna pk negativi, quindi la collisione
con una targa reale e' impossibile, non improbabile.

SHA-256 e non hash(): hash() e' randomizzato per processo (PYTHONHASHSEED), quindi
darebbe una targa diversa a ogni riavvio del worker -> una riga duplicata per ogni
riavvio. E non crc32: 32 bit collidono con probabilita' ~10^-3 su 3000 contatti, in
uno spazio che GlobalContact condivide fra TUTTE le campagne.
"""
from __future__ import annotations

import hashlib

# 63 bit: il valore negato sta sempre in un BIGINT firmato.
_MASCHERA = (1 << 63) - 1


def normalizza_username(username: str) -> str:
    """Minuscolo, senza chiocciola iniziale, senza spazi ai bordi.

    La chiocciola non e' teorica: alcuni account in DB hanno lo username salvato
    come '@michele.carozza'.
    """
    return (username or "").strip().lstrip("@").lower()


def targa_provvisoria(username: str) -> int:
    """Numero negativo stabile derivato dallo username. Mai zero."""
    normale = normalizza_username(username)
    digest = hashlib.sha256(normale.encode("utf-8")).digest()
    valore = int.from_bytes(digest[:8], "big") & _MASCHERA
    return -(valore or 1)   # il caso valore==0 e' irraggiungibile in pratica, ma 0 non e' negativo


def e_provvisoria(ig_user_id: int | None) -> bool:
    """True se la targa e' una nostra provvisoria (negativa), non un pk reale."""
    return ig_user_id is not None and ig_user_id < 0
