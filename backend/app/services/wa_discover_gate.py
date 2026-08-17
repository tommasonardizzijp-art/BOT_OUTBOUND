"""Le guardie che decidono se una scansione puo' partire adesso.

Tutte fail-closed e in ordine dalla piu' economica alla piu' costosa. Il
chiamante riceve un CODICE, non un booleano: la UI deve poter dire perche' no,
e "Errore 409" non dice a nessuno cosa fare dopo.

`puo_lanciare` stessa non committa mai (lo fa il chiamante, dopo `apri_run`):
ma NON e' piu' una funzione di sola lettura, perche' chiama
`wa_discover_runs.chiudi_se_orfana`, che ha un effetto collaterale reale --
chiude una run rimasta 'running' oltre ogni tempo credibile -- e lo fa con
una sessione PROPRIA che committa lei stessa (Task 10). E' una scelta
deliberata: far dipendere quella guarigione dal commit del chiamante
l'avrebbe persa ogni volta che una guardia successiva rifiuta comunque.
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
    "commit_insufficiente": (
        "Memoria di sistema quasi esaurita (troppe applicazioni aperte): chiudi "
        "qualche finestra e riprova. Aprire un browser adesso rischia di far "
        "cadere Redis, non solo di far fallire la scansione."),
}


def ram_libera_mb() -> int:
    """RAM FISICA disponibile in MB. Funzione a se' per poterla sostituire nei test."""
    return int(psutil.virtual_memory().available / (1024 * 1024))


def commit_disponibile_mb() -> int | None:
    """Commit di sistema ancora concedibile, in MB. `None` se non misurabile.

    NON e' la RAM fisica libera, ed e' la distinzione che conta: Windows concede
    memoria fino al *commit limit* (RAM + pagefile), e quando quel tetto si
    avvicina le richieste vengono rifiutate anche se la RAM fisica sembra
    respirare. Il 17/08 e' esattamente cosi' che si e' fermato il bot: Memurai ha
    chiesto la sua riserva per il salvataggio periodico, se l'e' vista negare
    (`0x5af`, "The paging file is too small"), ed e' rimasto appeso -- campagna
    ferma 90 minuti. La RAM fisica libera in quel momento non era il segnale.

    Si legge da `GlobalMemoryStatusEx` e non da psutil: `psutil.swap_memory()`
    su Windows riporta il pagefile, che e' un'altra cosa. Misurato insieme sulla
    stessa macchina: commit davvero disponibile 12044 MB, `swap_memory().free`
    20395 MB. Fidarsi del secondo significherebbe credere di avere 8 GB che non
    ci sono.

    Ritorna `None` fuori da Windows (il chiamante salta il controllo) invece di
    inventare un equivalente: su Linux il concetto vive in `Committed_AS`, con
    semantica diversa a seconda dell'overcommit, e una traduzione approssimata
    qui varrebbe meno di un controllo assente e dichiarato.
    """
    import ctypes

    if not hasattr(ctypes, "windll"):
        return None

    class _MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    stato = _MemoryStatusEx()
    stato.dwLength = ctypes.sizeof(stato)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stato)):
        # Non si tratta come "zero disponibile": un sensore rotto che fa
        # rifiutare tutto spegne la funzione invece di proteggerla.
        logger.warning("[WaDiscoverGate] GlobalMemoryStatusEx fallita: "
                       "controllo commit saltato")
        return None
    return int(stato.ullAvailPageFile / (1024 * 1024))


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

    # Secondo controllo, su una risorsa DIVERSA dalla prima: vedi
    # `commit_disponibile_mb`. Va dopo perche' e' quello che si puo' saltare
    # (None fuori da Windows), non perche' conti meno -- il guasto del 17/08 e'
    # passato proprio di qui mentre la RAM fisica sembrava a posto.
    commit = commit_disponibile_mb()
    if commit is not None and commit < settings.wa_discover_commit_min_mb:
        return "commit_insufficiente"

    return None
