"""Pseudonimizzazione dei numeri di telefono (SDD P12).

La chiave interna di un contatto WhatsApp e' l'HMAC del suo numero, mai il
numero. Il numero in chiaro esiste in due soli posti: cifrato con Fernet a DB
(`encrypted_phone`, decifrato solo al momento di aprire la chat) e nella
memoria del processo per la durata dell'invio.

HMAC e non SHA-256 nudo: lo spazio dei numeri italiani e' piccolo abbastanza da
essere invertito con un dizionario in minuti. Serve una chiave segreta.
"""
import hmac
import re
from hashlib import sha256

from app.config import settings

# Marcatori di direzione del testo che WhatsApp infila negli attributi `title`.
# Invisibili a schermo, ma un numero che li contiene non normalizza (misurato
# in M0, 27/07).
_BIDI = re.compile(r"[‪-‮⁦-⁩]")
_NON_CIFRE = re.compile(r"[^\d+]")


class PhoneNormalizationError(ValueError):
    """Il numero non e' normalizzabile in E.164.

    Deliberatamente un'eccezione e non un valore di ritorno None: un numero
    "quasi giusto" non esiste. Chi normalizza scrivera' a quel numero, e
    indovinare significa scrivere a uno sconosciuto.
    """


def normalize_e164(raw: str, default_country: str = "39") -> str:
    """Da qualunque forma scritta da un umano a `393421460077` (senza '+')."""
    if not raw or not isinstance(raw, str):
        raise PhoneNormalizationError(f"numero vuoto o non testuale: {raw!r}")

    s = _NON_CIFRE.sub("", _BIDI.sub("", raw).strip())
    if s.startswith("+"):
        s = s[1:]
    elif s.startswith("00"):
        s = s[2:]
    s = s.replace("+", "")

    if not s.isdigit():
        raise PhoneNormalizationError(f"caratteri non numerici: {raw!r}")
    # Numero nazionale italiano: il prefisso lo mettiamo noi.
    if len(s) == 10 and s.startswith("3"):
        s = default_country + s
    if not (11 <= len(s) <= 15):
        raise PhoneNormalizationError(f"lunghezza fuori range E.164 ({len(s)}): {raw!r}")
    return s


def hmac_phone(e164: str) -> str:
    """HMAC-SHA256 esadecimale del numero normalizzato."""
    key = (settings.phone_hmac_key or "").encode("utf-8")
    if not key:
        raise RuntimeError(
            "PHONE_HMAC_KEY non impostata: senza, gli pseudonimi sarebbero "
            "invertibili con un dizionario. Genera con: "
            'python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    return hmac.new(key, e164.encode("utf-8"), sha256).hexdigest()


def mask_phone(e164: str) -> str:
    """`+39•••••077` — forma da log e da display admin.

    Non solleva mai: finisce dentro i messaggi d'errore, e un'eccezione qui
    nasconderebbe l'errore vero che si stava per loggare.
    """
    if not e164:
        return ""
    s = str(e164).lstrip("+")
    prefisso, resto = s[:2], s[2:]
    return f"+{prefisso}" + "•" * 5 + resto[-3:]
