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
import re as _re

# 63 bit: il valore negato sta sempre in un BIGINT firmato.
_MASCHERA = (1 << 63) - 1


def normalizza_username(username: str) -> str:
    """Minuscolo, senza chiocciola iniziale, senza spazi ai bordi.

    La chiocciola non e' teorica: alcuni account in DB hanno lo username salvato
    come '@michele.carozza'.
    """
    return (username or "").strip().lstrip("@").lower()


# Instagram ammette lettere, cifre, punto e underscore. Niente spazi, niente
# trattini. Max 30 caratteri.
_FORMA_HANDLE = _re.compile(r"^[a-z0-9._]{1,30}$")


def handle_valido(username: str | None) -> bool:
    """True se la stringa ha la forma di uno username Instagram reale.

    Serve a tenere fuori dalla chiave d'identita' i SEGNAPOSTO dei profili chiusi
    o disattivati, che Instagram mostra uguali per tutti ("Utente di Instagram",
    "Instagram User", e l'equivalente in ogni altra lingua) e che finiscono nel
    campo username (log reale 22/08: `[InboxLista] @utente instagram ...`).

    Perche' la FORMA e non l'insieme dei segnaposto: `e_segnaposto`
    (inbox_browser/testo.py) confronta con `LINGUE`, che contiene solo IT e EN, e
    il segnaposto dipende dalla lingua dell'interfaccia dell'ACCOUNT — non nostra.
    Su un account in spagnolo quel filtro non scatta e nessun errore lo segnala.
    Uno spazio in mezzo invece esclude un handle in QUALUNQUE lingua.

    Finche' la chiave era il pk questo non mordeva: N profili chiusi diventavano N
    righe distinte, brutte ma separate. Con lo username chiave, senza questo
    controllo collasserebbero tutti in un contatto solo, mescolando cronologia e
    contatti di persone diverse. E sarebbero comunque righe morte: l'invio naviga
    su `instagram.com/<username>/`, quindi un contatto che si chiama
    "utente instagram" non ricevera' mai un DM.
    """
    return bool(_FORMA_HANDLE.match(normalizza_username(username)))


def targa_provvisoria(username: str) -> int:
    """Numero negativo stabile derivato dallo username. Mai zero."""
    normale = normalizza_username(username)
    digest = hashlib.sha256(normale.encode("utf-8")).digest()
    valore = int.from_bytes(digest[:8], "big") & _MASCHERA
    return -(valore or 1)   # il caso valore==0 e' irraggiungibile in pratica, ma 0 non e' negativo


def e_provvisoria(ig_user_id: int | None) -> bool:
    """True se la targa e' una nostra provvisoria (negativa), non un pk reale."""
    return ig_user_id is not None and ig_user_id < 0
