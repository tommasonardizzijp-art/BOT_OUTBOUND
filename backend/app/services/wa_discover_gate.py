"""Le guardie che decidono se una scansione puo' partire adesso.

Tutte fail-closed e in ordine dalla piu' economica alla piu' costosa. Il
chiamante riceve un CODICE, non un booleano: la UI deve poter dire perche' no,
e "Errore 409" non dice a nessuno cosa fare dopo.
"""
from __future__ import annotations

import psutil

from app.config import settings
from app.models.wa import WaNumberStatus
from app.services import bot_state_service, wa_discover_runs, wa_profile_lock

MESSAGGI = {
    "numero_non_attivo": (
        "Il numero non e' attivo: collegalo con Avvia login QR prima di scansionare."),
    "canale_fermo": (
        "Il canale WhatsApp e' fermo (kill-switch alzato): riprendilo dalla "
        "striscia in alto prima di scansionare."),
    "browser_occupato": (
        "Un altro numero sta gia' usando il browser sulla macchina del backend. "
        "Su questo PC ne gira uno solo per volta: riprova fra qualche minuto."),
    "scan_gia_in_corso": (
        "Una scansione su questo numero e' gia' in corso: aspetta che finisca."),
    "ram_insufficiente": (
        "Memoria insufficiente per aprire un browser: chiudi qualche finestra "
        "e riprova."),
}


def ram_libera_mb() -> int:
    """RAM disponibile in MB. Funzione a se' per poterla sostituire nei test."""
    return int(psutil.virtual_memory().available / (1024 * 1024))


async def puo_lanciare(db, number) -> str | None:
    """None se si puo' partire, altrimenti il codice del rifiuto."""
    if number.status != WaNumberStatus.active:
        return "numero_non_attivo"

    if await bot_state_service.is_wa_halted(db):
        return "canale_fermo"

    # Gate GLOBALE, non per-numero: vale anche se il lucchetto e' di un altro
    # numero, perche' la risorsa scarsa e' la RAM della macchina.
    if await wa_profile_lock.profilo_occupato_da() is not None:
        return "browser_occupato"

    if await wa_discover_runs.run_attiva(db, number.id) is not None:
        return "scan_gia_in_corso"

    if ram_libera_mb() < settings.wa_discover_ram_min_mb:
        return "ram_insufficiente"

    return None
