"""Funzioni pure dei PoC WhatsApp (M0). Nessun import di app.*, nessun I/O di rete.

Sono qui perche' sono le uniche parti verificabili senza un browser: le altre
dipendono dal DOM di WhatsApp Web, che e' esattamente cio' che il PoC deve scoprire.
"""
import os
import re

DEFAULT_CC = "39"
MIN_DIGITS = 8   # sotto questa soglia non e' un numero mobile plausibile
MAX_DIGITS = 15  # E.164

# Parole/frasi di opt-out. MVP italiano (Q6). Il falso positivo e' accettato:
# meglio un opt-out di troppo che uno mancato (SDD 7.5 punto 6).
_STOP_PATTERNS = [
    r"\bstop\b",
    r"\bbasta\b",
    r"\bcancellami\b",
    r"\bdisiscrivimi\b",
    r"\bnon\s+scrivermi(\s+piu)?\b",
    r"\bnon\s+voglio\s+piu\s+ricevere\b",
    r"\brimuovimi\b",
]
_STOP_RE = re.compile("|".join(_STOP_PATTERNS), re.IGNORECASE)

# Numero di telefono in un dump: 6+ cifre, tollerando UN separatore
# (spazio, punto, trattino, slash) tra le cifre — WhatsApp mostra
# "342 146 0077", non "3421460077". Falso positivo accettato: date e
# prezzi lunghi finiscono in <num>, su artefatti di debug va bene.
_NUM_RE = re.compile(r"\d(?:[\s.\-/]?\d){5,}")


class NotAllowed(Exception):
    """Tentato invio verso un numero non in allowlist. In M0 e' un errore fatale."""


def normalize_e164(raw: str, default_cc: str = DEFAULT_CC) -> str | None:
    """'+39 342 146 0077' -> '393421460077' (cifre pure, formato deep-link WhatsApp).

    Restituisce None se l'input non e' un numero plausibile: chi chiama deve
    trattare il None come scarto, mai come 'numero vuoto'.
    """
    if not raw:
        return None
    s = raw.strip()
    has_plus = s.startswith("+") or s.startswith("00")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    if digits.startswith("00"):
        digits = digits[2:]
    elif not has_plus and not digits.startswith(default_cc):
        digits = default_cc + digits
    if not (MIN_DIGITS <= len(digits) <= MAX_DIGITS):
        return None
    return digits


def contains_stop(text: str) -> bool:
    """True se il testo contiene una richiesta di opt-out (parole intere)."""
    if not text:
        return False
    return bool(_STOP_RE.search(text))


def mask_pii(text: str, keep: int = 40) -> str:
    """Maschera i numeri e tronca: i dump di M0 girano su chat di clienti veri."""
    if text is None:
        return ""
    masked = _NUM_RE.sub("<num>", text)
    if len(masked) > keep:
        return masked[:keep] + "..."
    return masked


class AllowList:
    """Guardia bloccante degli invii M0 (vincolo Q60).

    Fail-closed per costruzione: se la variabile non e' configurata l'insieme e'
    vuoto e NESSUN numero passa. E' l'unica cosa che sta tra questo PoC e un
    messaggio di prova mandato a un cliente vero di Primero.
    """

    def __init__(self, numbers: set[str]):
        self._numbers = numbers

    @classmethod
    def load(cls, env_var: str = "POC_WA_ALLOWED_NUMBERS") -> "AllowList":
        raw = os.environ.get(env_var, "")
        nums = set()
        for chunk in raw.split(","):
            n = normalize_e164(chunk)
            if n:
                nums.add(n)
        return cls(nums)

    def is_allowed(self, e164: str) -> bool:
        return bool(e164) and e164 in self._numbers

    def assert_allowed(self, e164: str) -> None:
        if not self.is_allowed(e164):
            raise NotAllowed(
                f"Numero non in allowlist (ultime 4 cifre: …{str(e164)[-4:]}). "
                f"In M0 si scrive SOLO a chat controllate: aggiungilo a "
                f"POC_WA_ALLOWED_NUMBERS oppure fermati."
            )

    def __len__(self) -> int:
        return len(self._numbers)
