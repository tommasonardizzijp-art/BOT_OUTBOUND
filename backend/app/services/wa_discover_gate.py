"""Le guardie che decidono se una scansione puo' partire adesso.

Tutte fail-closed e in ordine dalla piu' economica alla piu' costosa. Il
chiamante riceve un CODICE, non un booleano: la UI deve poter dire perche' no,
e "Errore 409" non dice a nessuno cosa fare dopo.
"""
from __future__ import annotations

import psutil
from loguru import logger

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
        "Il browser sulla macchina del backend e' gia' in uso (o non e' stato "
        "possibile verificarlo): su questo PC ne gira uno solo per volta, "
        "riprova fra qualche minuto."),
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
    try:
        occupato = await wa_profile_lock.profilo_occupato_da() is not None
    except Exception as exc:  # noqa: BLE001 -- fail-closed, vedi sotto
        # "Tutte fail-closed" (docstring del modulo) e Redis irraggiungibile
        # durante il gate e' gia' nella lista adversarial del piano: su
        # questa macchina non e' ipotetico, Memurai e' gia' andato giu' una
        # volta (12/08, ucciso da un taskkill /T). Senza questo except
        # l'eccezione risale fino all'endpoint e diventa un 500 al posto di
        # un 409 leggibile -- il contrario di "fail-closed, rifiuto leggibile".
        logger.warning(f"[WaDiscoverGate] profilo_occupato_da fallito "
                       f"({type(exc).__name__}: {exc}) -- non posso verificare "
                       "se il browser e' libero, tratto come occupato")
        occupato = True
    if occupato:
        return "browser_occupato"

    # Auto-guarigione: una run che nessuno ha chiuso non deve bloccare il
    # numero per sempre. Va prima del controllo qui sotto, altrimenti la
    # guardia rifiuterebbe basandosi su una run morta.
    await wa_discover_runs.chiudi_se_orfana(db, number.id)

    if await wa_discover_runs.run_attiva(db, number.id) is not None:
        return "scan_gia_in_corso"

    if ram_libera_mb() < settings.wa_discover_ram_min_mb:
        return "ram_insufficiente"

    return None
