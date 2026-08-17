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
_BIDI = re.compile(r"[\u202a-\u202e\u2066-\u2069]")

# Separatori tipografici ammessi in un numero scritto da un umano: spazio,
# trattino, punto, slash, parentesi. Lista CHIUSA: qualunque altro carattere
# NON va ripulito, deve far fallire il match sotto. Bug trovato in review
# (2026-07-28): con un `[^\d+]` che ripuliva le lettere ma non rifiutava
# l'input, le cifre di un'annotazione tipo "ext. 12" sopravvivevano e si
# saldavano al numero -> numero diverso da quello scritto, accettato in
# silenzio.
_SEPARATORI = re.compile(r"[ \-./()]")

# Cifre ASCII esplicite. `\d` e `str.isdigit()` accettano anche le cifre di
# altri alfabeti (es. arabo-indiche U+0660-U+0669): non sono componibili in
# E.164 e finirebbero a DB come hmac di un numero che non esiste.
_E164_ASCII = re.compile(r"^\+?[0-9]+$")


class PhoneNormalizationError(ValueError):
    """Il numero non e' normalizzabile in E.164.

    Deliberatamente un'eccezione e non un valore di ritorno None: un numero
    "quasi giusto" non esiste. Chi normalizza scrivera' a quel numero, e
    indovinare significa scrivere a uno sconosciuto.
    """


def normalize_e164(raw: str, default_country: str = "39") -> str:
    """Da qualunque forma scritta da un umano a `393421460077` (senza '+').

    Valida, non ripulisce: dopo aver tolto i marcatori bidi e i soli
    separatori tipografici della lista chiusa (_SEPARATORI), quello che
    resta deve corrispondere per intero a `^\\+?[0-9]+$`. Qualunque altro
    carattere (lettere, cifre non ASCII, punteggiatura non prevista) fa
    rifiutare l'intero input invece di essere silenziosamente scartato.
    """
    if not raw or not isinstance(raw, str):
        raise PhoneNormalizationError(f"numero vuoto o non testuale: {raw!r}")

    s = _SEPARATORI.sub("", _BIDI.sub("", raw).strip())

    if not _E164_ASCII.match(s):
        raise PhoneNormalizationError(
            f"attese solo cifre ASCII e separatori tipografici (spazio - . / ( )): {raw!r}"
        )

    if s.startswith("+"):
        s = s[1:]
    elif s.startswith("00"):
        s = s[2:]

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


def hmac_e164(cifre_senza_piu: str) -> str:
    """hmac_phone della forma canonica (CON '+'), a partire dalle cifre nude
    che `normalize_e164` restituisce.

    Un solo posto per non dimenticare di ricomporre il '+' prima di
    pseudonimizzare: dimenticarlo qui e' esattamente il difetto che ha reso
    invisibile l'opt-out il 12/08 (AVVIO 12/08 §1) -- wa_discover/salvataggio.py
    chiamava `hmac_phone(riga.numero)` sulle cifre nude, il reply-watcher
    cercava solo `hmac_phone("+" + cifre)`, e le due meta' non si
    incontravano mai. Ogni call-site che pseudonimizza un numero di un
    contatto (wa_ingest.py, wa_discover/salvataggio.py) passa da qui.
    """
    return hmac_phone("+" + cifre_senza_piu)


def mask_phone(e164: str) -> str:
    """Prefisso e ultime 3 cifre, il resto oscurato (es. +39, poi 5 pallini, poi 077).

    Non solleva mai: finisce dentro i messaggi d'errore, e un'eccezione qui
    nasconderebbe l'errore vero che si stava per loggare.
    """
    if not e164:
        return ""
    s = str(e164).lstrip("+")
    prefisso, resto = s[:2], s[2:]
    return f"+{prefisso}" + "\u2022" * 5 + resto[-3:]
