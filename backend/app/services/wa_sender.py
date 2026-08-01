"""Invio di UN messaggio WhatsApp: apertura chat, guardie, invio.

Il POM (whatsapp_page.py) espone segnali e non decide; la politica sta
qui. Ogni funzione di questo modulo che decide "si invia / non si invia"
e' pura o quasi, perche' deve essere provabile senza browser: le tre volte
in cui M1 ha sbagliato una guardia, il difetto era nel giudizio, non nel
DOM.
"""
from dataclasses import dataclass

from loguru import logger

from app.browser.whatsapp_page import OpenResult

# Segnali del POM che significano "la chat 1:1 non esiste": colpa del dato,
# non nostra. Copiati alla lettera da whatsapp_page.open_chat /
# _apri_chat_da_risultati / _history_signal: se cambiano li', questo modulo
# smette di riconoscerli e cade nel ramo fail-closed (colpa nostra), che e'
# il fallimento giusto.
_SEGNALI_CHAT_INESISTENTE = (
    "nessuna-cronologia:nessun-messaggio-nel-pannello",
    "nessuna-cronologia:sezione-chat-vuota:nessuna-conversazione-esistente",
    "nessuna-cronologia:nessuna-sezione-chat:solo-gruppi-o-contatti-senza-conversazione",
)

# Segnali che dicono "la pagina non era nello stato che ci aspettavamo":
# infrastruttura nostra. Il contatto NON si tocca.
_SEGNALI_COLPA_NOSTRA = (
    "nessuna-cronologia:casella-ricerca-non-trovata",
    "nessuna-cronologia:ricerca-non-svuotata",
    "nessuna-cronologia:focus-non-sulla-ricerca-pre-invio",
)

_SEGNALE_RICERCA_VUOTA = "nessuna-cronologia:nessun-risultato-di-ricerca"


@dataclass
class EsitoApertura:
    puo_inviare: bool
    esito_contatto: str | None   # 'skipped' | None (None = non si tocca lo stato)
    motivo: str
    colpa_nostra: bool           # True -> conta verso l'escalation FM2 del numero


def valuta_apertura(res: OpenResult) -> EsitoApertura:
    """Traduce (ok, signal) del POM nella decisione. Contratto §3.1-3.2.

    Fail-closed su tutto cio' che non e' riconosciuto: un segnale nuovo
    (POM aggiornato, WhatsApp cambiato) non deve mai finire nel ramo che
    marca il contatto, perche' quello e' irreversibile per il contatto e
    invisibile a chi guarda i log.
    """
    signal = res.signal or ""

    if res.ok and signal.startswith("cronologia:"):
        try:
            n = int(signal.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            logger.error(f"valuta_apertura: conteggio illeggibile in {signal!r} -- "
                         "non si invia")
            return EsitoApertura(False, None, "segnale_illeggibile", True)
        if n >= 1:
            return EsitoApertura(True, None, f"cronologia:{n}", False)
        return EsitoApertura(False, None, "cronologia_vuota", True)

    if signal in _SEGNALI_CHAT_INESISTENTE:
        return EsitoApertura(False, "skipped", "no_existing_chat", False)

    if signal in _SEGNALI_COLPA_NOSTRA:
        return EsitoApertura(False, None, signal.split(":", 1)[1], True)

    if signal == _SEGNALE_RICERCA_VUOTA:
        # Ambiguo: lo scioglie il chiamante col contesto di sessione (§3.3).
        return EsitoApertura(False, None, "ricerca_senza_risultati", False)

    logger.error(f"valuta_apertura: segnale non catalogato {signal!r} -- "
                 "trattato come guasto nostro, il contatto non si tocca")
    return EsitoApertura(False, None, "segnale_non_catalogato", True)
