"""Stato della sessione WhatsApp di un numero, e login assistito locale.

DEPLOYMENT (decisione 27/07): il browser gira sul PC di Tommaso, il QR si
inquadra di persona. Niente pagina admin per il QR da remoto. Ma la funzione
di login sta dietro un'interfaccia perche' "in futuro lo eseguono i clienti a
casa loro" e' uno scenario dichiarato.

DUE COSE MISURATE IN M0 CHE QUESTO MODULO DEVE RISPETTARE:

1. Il PROCESSO browser puo' morire senza che muoia la SESSIONE WhatsApp.
   Successo il 27/07: browser caduto dopo 16 minuti, ma alla riapertura la
   lista chat era li' e nessun QR e' stato chiesto. Quindi un browser morto
   NON significa qr_required: si riapre e si guarda, non si allarma il cliente.
   check_session() e' esattamente questo: riapre il profilo persistente e
   guarda cosa c'e', non deduce nulla dal fatto che il processo precedente
   non ci fosse piu'.

2. Un profilo si apre UNA VOLTA SOLA. Aprire un secondo browser sullo stesso
   user-data-dir e' il modo piu' rapido di corrompere il profilo e perdere la
   sessione -- cioe' provocare proprio il re-scan che si vuole evitare.
   Da qui il lock per-numero (riuso di _get_account_lock, app/browser/
   context_manager.py:32, con chiave `wa_<number_id>` per non collidere con
   gli account_id Instagram nello stesso dizionario condiviso).
"""
import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from loguru import logger

from app.browser.whatsapp_page import WhatsAppWebPage
from app.models.wa import WaNumberStatus

# URL fisso: WhatsApp Web non ha un dominio per-numero, la sessione vive nel
# profilo Chromium persistente (cookie/localStorage), non nell'URL.
WHATSAPP_WEB_URL = "https://web.whatsapp.com/"

# Pausa fra un giro e l'altro del loop di assisted_login mentre si aspetta lo
# scan del QR. Non e' una misura M0.
#
# ATTENZIONE, questo valore NON governa la reattivita' del loop, ed e' bene
# saperlo prima di provare a "renderlo piu' reattivo" abbassandolo. Il costo
# di un giro e' dominato da session_state(), che prova prima CHATLIST con
# SESSION_STATE_TIMEOUT_CHATLIST_MS (90 s, tarato per tollerare un profilo
# freddo) e solo dopo il QR: mentre si aspetta lo scan la lista chat NON c'e',
# quindi ogni giro paga quel timeout per intero. Con timeout_s=180 si fanno
# una o due letture reali, non novanta.
#
# Non e' un errore di classificazione -- cio' che viene letto e persistito e'
# comunque vero -- ma se servira' un login davvero reattivo la leva e' una
# lettura DOM piu' snella della session_state() generica, non questa costante.
#
# DECISIONE DI CHIUSURA MODULO (team lead, 28/07): si lascia com'e' in M1.
# Non c'e' nessun invio e nessun operatore esposto al login assistito su
# scala (e' una persona che inquadra un QR, non un flusso automatizzato che
# deve essere reattivo). Se in futuro servira' un login davvero reattivo, la
# leva resta quella sopra -- una lettura DOM piu' snella di session_state()
# -- non abbassare questa costante.
ASSISTED_LOGIN_POLL_INTERVAL_S = 2.0


class WaBrowserLoopUnsupported(RuntimeError):
    """L'event loop che ospita la chiamata non sa avviare subprocess, quindi
    nessun browser puo' partire da qui.

    Non e' un guasto del canale WhatsApp: e' come e' stato avviato il
    processo. Su Windows `asyncio.SelectorEventLoop` non implementa
    `_make_subprocess_transport`, e Patchright avvia il driver Node proprio
    come subprocess -- quindi `async_playwright()` muore con un
    `NotImplementedError` nudo, senza una riga che dica da dove viene.

    Chi sceglie il loop e' uvicorn, non noi (uvicorn/loops/asyncio.py):

        if sys.platform == "win32" and not use_subprocess:
            return asyncio.ProactorEventLoop
        return asyncio.SelectorEventLoop

    e `use_subprocess` e' `reload or workers > 1` (uvicorn/config.py). Quindi
    **`uvicorn --reload` su Windows disattiva il login QR e la verifica
    sessione**, le due sole azioni che aprono un browser dentro il processo
    web. Gli invii non sono toccati: girano negli ARQ worker, processi a
    parte con il loop di default (Proactor).

    Costato una sessione di collaudo il 08/08: dal terminale funzionava
    tutto, dall'interfaccia usciva solo "Errore interno temporaneo del
    server" -- il 500 generico di _CatchUnhandledMiddleware. Questa
    eccezione esiste per dire il perche' al primo colpo, invece di lasciare
    ripetere quella diagnosi.
    """


# Su piattaforme non-Windows la classe non esiste: qui interessa solo per la
# isinstance sotto, e l'assenza vale "nessun vincolo da controllare".
_ProactorEventLoop = getattr(asyncio, "ProactorEventLoop", None)


def _loop_puo_avviare_subprocess(loop, piattaforma: str | None = None) -> bool:
    """True se `loop` puo' lanciare un subprocess (quindi un browser).

    Criterio POSITIVO -- deve essere un ProactorEventLoop -- non "non e' un
    SelectorEventLoop": su Windows l'unico loop capace di subprocess e' il
    Proactor, e un loop di terze parti che non lo sia fallirebbe uguale.
    Meglio un falso allarme (diagnosticabile) di un NotImplementedError.

    `piattaforma` e' iniettabile per i test: la logica va verificata anche
    dalla CI Linux, dove `asyncio.ProactorEventLoop` non esiste proprio.

    E quando la classe non c'e' ma la piattaforma dichiarata e' win32, la
    risposta e' False, non True. Su un Windows vero quella classe c'e'
    sempre: la combinazione si produce solo iniettando `piattaforma` da
    un'altra macchina, cioe' in un test. Rispondere True li' rendeva il caso
    win32 non verificabile dalla CI Linux -- il guard usciva prima di
    guardare il loop, e il test "un loop qualunque su win32 non va bene"
    passava per la ragione sbagliata (falliva in CI, che e' come e' emerso).
    False e' anche la risposta coerente col resto del modulo: non poter
    verificare non e' un permesso.
    """
    piattaforma = sys.platform if piattaforma is None else piattaforma
    if piattaforma != "win32":
        return True
    return _ProactorEventLoop is not None and isinstance(loop, _ProactorEventLoop)


def _verifica_loop_o_solleva() -> None:
    if _loop_puo_avviare_subprocess(asyncio.get_running_loop()):
        return
    raise WaBrowserLoopUnsupported(
        "Impossibile aprire il browser WhatsApp: il processo gira su un "
        "event loop che su Windows non sa avviare subprocess "
        "(asyncio.SelectorEventLoop). E' cosi' quando uvicorn parte con "
        "--reload o --workers>1. Riavviare il backend SENZA --reload: "
        "uvicorn app.main:app --port 8000"
    )


def stato_da_segnale(segnale: str) -> WaNumberStatus:
    """Mappa il segnale del POM sullo stato del numero (SDD 8.3).

    'unknown' -> disconnected, MAI active: e' una schermata che non abbiamo
    riconosciuto (interstitial, aggiornamento, ban). Trattarla da sessione
    valida farebbe partire gli invii contro una pagina che non e' WhatsApp.
    """
    return {
        "logged_in": WaNumberStatus.active,
        "qr_required": WaNumberStatus.qr_required,
    }.get(segnale, WaNumberStatus.disconnected)


def profile_dir_for(number_id: str) -> Path:
    """Path del profilo Chromium persistente per il numero (convenzione
    data/browser_profiles/wa_<id>). Prefisso wa_ per stare nella stessa
    cartella dei profili Instagram (browser_profiles_dir) senza collidere
    con un account_id che avesse lo stesso UUID.

    number_id e' sempre un UUID generato da noi (WaNumber.id) -- non arriva
    mai da input utente -- ma la funzione e' comunque a guardia: '/' e '\\'
    dentro f"wa_{number_id}" sono interpretati da pathlib come separatori di
    path, quindi un number_id tipo '../../etc' produce un path FUORI dalla
    cartella prevista (misurato in review 28/07). Rifiutare qui e' piu'
    robusto che affidarsi a resolve()+contenimento: chiude anche i casi che
    restano dentro browser_profiles_dir ma fuori dallo schema wa_<id>.
    """
    from app.config import settings

    if "/" in number_id or "\\" in number_id or ".." in number_id:
        raise ValueError(f"number_id non valido per un path di profilo: {number_id!r}")

    return Path(settings.browser_profiles_dir) / f"wa_{number_id}"


def _get_wa_lock(number_id: str) -> asyncio.Lock:
    """Lock per-numero: stessa forma di _get_account_lock (app/browser/
    context_manager.py:32), chiave namespaced wa_<number_id> per condividere
    il dizionario dei lock senza rischiare collisioni con gli account_id
    Instagram."""
    from app.browser.context_manager import _get_account_lock

    return _get_account_lock(f"wa_{number_id}")


def _mask_proxy_url(url: str) -> str:
    """Maschera user:pass di un proxy URL prima di metterlo in un messaggio
    d'errore. Tiene schema/host/porta/path (utile per capire QUALE proxy e'
    rotto), oscura le credenziali: e' proprio il caso di un proxy malformato
    quello in cui qualcuno legge il log, e le credenziali in chiaro in un
    ValueError sono le stesse credenziali in chiaro nei log dell'app.

    Non solleva mai: deve restare leggibile anche sull'input piu' rotto
    possibile (e' gia' dentro il ramo che gestisce un URL non valido).
    """
    from urllib.parse import urlparse

    try:
        p = urlparse(url.strip())
    except Exception:
        return "***"

    schema = f"{p.scheme}://" if p.scheme else ""
    cred = "***@" if (p.username or p.password) else ""
    host = p.hostname or ""
    porta = f":{p.port}" if p.port else ""
    resto = f"{schema}{cred}{host}{porta}{p.path or ''}"
    return resto if resto else "***"


@asynccontextmanager
async def _open_wa_browser(number_id: str, *, headless: bool, proxy_url: str | None):
    """Apre il profilo Chromium persistente del numero WA. Un solo browser
    per numero alla volta (lock, vedi modulo docstring punto 2): il secondo
    launch sullo stesso user-data-dir e' cio' che corrompe il profilo.

    Stesso schema di app.browser.context_manager.get_browser_context, non
    riusato direttamente: quella funzione e' cablata su InstagramAccount
    (fetch proxy dalla tabella account, profilo = account_id senza prefisso).
    Qui il proxy arriva gia' risolto dal chiamante (letto da WaNumber) e il
    profilo e' wa_<id> (profile_dir_for).
    """
    # PRIMA di tutto il resto, e per lo stesso motivo per cui la validazione
    # del proxy sta fuori dal lock (commento sotto): se il browser non puo'
    # partire in questo processo, va detto senza aver toccato il profilo --
    # ne' mkdir, ne' rimozione dei SingletonLock.
    _verifica_loop_o_solleva()

    try:
        from patchright.async_api import async_playwright
    except ImportError:
        raise ImportError(
            "Patchright is not installed. Run: pip install patchright && patchright install chromium"
        )

    from app.browser.context_manager import _build_fingerprint_script, parse_proxy_url
    from app.browser.fingerprint import get_fingerprint

    profile_dir = profile_dir_for(number_id)

    # La validazione del proxy resta QUI, fuori dal lock: e' pura (non tocca
    # il disco del profilo) e deve sollevare PRIMA di prendere il lock e
    # prima di toccare il profilo (test_adv38/39). Diverso da
    # context_manager.get_browser_context, dove tutto _prepare_launch -- proxy
    # compresa -- sta dentro il lock: li' il proxy arriva da una query DB
    # fatta dentro la sezione critica; qui arriva gia' risolto dal chiamante,
    # quindi non c'e' motivo di tenere una validazione pura dietro un lock
    # che non le serve.
    fingerprint = get_fingerprint(number_id)
    proxy_cfg = parse_proxy_url(proxy_url)
    if proxy_url and not proxy_cfg:
        raise ValueError(
            f"Proxy malformato per numero WA {number_id}: {_mask_proxy_url(proxy_url)!r}"
        )

    # Chromium_args tenuti ALLINEATI a context_manager._prepare_launch di
    # proposito (stesso schema, docstring del modulo): se in futuro
    # divergono da li', deve essere una scelta motivata con un commento qui
    # accanto, non una deriva silenziosa (--js-flags=--harmony mancava per
    # deriva, non per scelta -- review 28/07).
    chromium_args = [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--js-flags=--harmony",
    ]
    launch_kwargs = dict(
        user_data_dir=str(profile_dir),
        headless=headless,
        viewport=fingerprint["viewport"],
        user_agent=fingerprint["user_agent"],
        locale=fingerprint["locale"],
        timezone_id=fingerprint["timezone_id"],
        args=chromium_args,
        ignore_default_args=["--enable-automation"],
    )
    if proxy_cfg:
        launch_kwargs["proxy"] = proxy_cfg
    else:
        # Stesso avviso di context_manager: senza proxy il traffico esce
        # dall'IP locale. Per il canale WhatsApp conta piu' che per IG --
        # i proxy mobili servono a evitare che piu' numeri risultino
        # correlati dallo stesso indirizzo (SDD 10.2, minaccia T3). Senza
        # questa riga il fatto sarebbe invisibile nei log.
        logger.warning(
            f"wa_session({number_id}): browser SENZA proxy, il traffico esce "
            f"dall'IP locale"
        )
        chromium_args.append("--no-proxy-server")

    lock = _get_wa_lock(number_id)
    async with lock:
        # mkdir + pulizia lock-file DENTRO la sezione critica (CRITICAL,
        # review whole-branch 28/07): se un altro browser e' vivo su questo
        # profilo, il suo SingletonLock e' VERO, non stale -- cancellarlo
        # fuori da un lock gli toglie l'esclusivita' sul proprio
        # user_data_dir MENTRE lo sta usando, esattamente la corruzione del
        # profilo che il lock esiste per impedire (docstring del modulo,
        # punto 2). Stesso ordine di context_manager.get_browser_context:
        # li' il lock viene preso PRIMA di chiamare _prepare_launch, che
        # contiene la stessa pulizia (context_manager.py:97-104). Qui la
        # pulizia era rimasta fuori dal lock per errore, non per scelta:
        # nessun commento la giustificava, ed e' la stessa causa del re-scan
        # del QR che M1 esiste per evitare.
        profile_dir.mkdir(parents=True, exist_ok=True)
        for lock_file in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            lock_path = profile_dir / lock_file
            try:
                if lock_path.exists():
                    os.remove(lock_path)
            except OSError as e:
                logger.warning(f"wa_session: impossibile rimuovere {lock_file} per {number_id}: {e}")

        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(**launch_kwargs)
            await context.add_init_script(_build_fingerprint_script(fingerprint))
            try:
                yield context
            finally:
                await context.close()


async def _wa_number_or_raise(db, number_id: str):
    from sqlalchemy import select

    from app.models.wa import WaNumber

    r = await db.execute(select(WaNumber).where(WaNumber.id == number_id))
    numero = r.scalar_one_or_none()
    if numero is None:
        raise ValueError(f"Numero WA non trovato: {number_id}")
    return numero



# Stati che un HEALTH-CHECK non deve mai poter cancellare (adversarial 34,
# decisione presa dal team lead 28/07): retired/suspended li mette un
# operatore o la piattaforma, non sono deducibili da una lettura del DOM.
# Vedere 'logged_in' nel browser dice "la sessione e' viva", non "questo
# numero e' di nuovo operativo" -- quella e' una scelta esplicita, non un
# effetto collaterale di check_session/assisted_login che leggono un segnale.
_STATI_PROTETTI_DA_RESURREZIONE = frozenset({WaNumberStatus.retired, WaNumberStatus.suspended})


async def _persist_status(number_id: str, stato: WaNumberStatus, *,
                          da_lettura_automatica: bool = True) -> None:
    from app.database import AsyncSessionLocal
    from app.utils.tempo import adesso_utc

    async with AsyncSessionLocal() as db:
        numero = await _wa_number_or_raise(db, number_id)
        # Solo una LETTURA automatica non puo' togliere un cooldown. Un
        # operatore che riscansiona il QR di persona (assisted_login) sta
        # facendo l'azione esplicita di cui parla il commento su
        # _STATI_PROTETTI_DA_RESURREZIONE qui sopra, e deve poterlo rimettere
        # in gioco: bloccarlo anche a lui lascerebbe come unica uscita la
        # cancellazione a mano di una chiave Redis (review 07/08 su M5.1).
        promozione_da_cooldown = (da_lettura_automatica
                                  and numero.status == WaNumberStatus.cooldown
                                  and stato == WaNumberStatus.active)
        if numero.status in _STATI_PROTETTI_DA_RESURREZIONE and stato != numero.status:
            # Diagnostica comunque valida (un check_session su un numero
            # ritirato resta legittimo per capire se la sessione e' ancora
            # viva), ma NON e' la resurrezione: quella richiede un'azione
            # esplicita di un operatore, non un side-effect di una lettura.
            logger.warning(
                f"wa_session({number_id}): segnale={stato.value} letto ma stato "
                f"resta {numero.status.value} (protetto da resurrezione automatica)"
            )
        elif promozione_da_cooldown:
            # Un cooldown lo toglie la SCADENZA del suo timer
            # (wa_number_manager.release_expired_wa_cooldowns, che scrive con
            # una UPDATE diretta e non passa di qui), non una lettura del DOM.
            # Senza questo ramo l'health-check -- che gira ogni 30 minuti e
            # include i numeri in cooldown -- vedeva la sessione viva e
            # rimetteva il numero 'active': lo stop di 4 ore imposto da FM2
            # durava mezz'ora (review 07/08, difetto G1). E' la porta gemella
            # di quella gia' chiusa dentro wa_worker._ferma_numero_per_guasto,
            # dove il commento spiega perche' la chiave Redis va scritta.
            #
            # Si blocca SOLO la promozione. Un numero in cooldown che nel
            # frattempo ha perso la sessione deve poter diventare
            # disconnected/qr_required: quella e' informazione vera, e senza di
            # essa il cron non metterebbe in pausa le sue campagne.
            logger.info(
                f"wa_session({number_id}): sessione viva, ma il numero resta in "
                "cooldown -- lo toglie la scadenza del timer, non un health-check")
        else:
            numero.status = stato
        numero.session_checked_at = adesso_utc()
        numero.browser_profile = str(profile_dir_for(number_id))
        await db.commit()


async def check_session(number_id: str) -> WaNumberStatus:
    """Health-check: riapre il profilo del numero (headless) e guarda cosa
    c'e'. Non presuppone nulla dal fatto che l'ultimo processo browser sia
    morto (modulo docstring punto 1) -- il segnale viene SEMPRE da una
    lettura fresca del DOM, mai da uno stato in memoria di un run precedente.
    """
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        numero = await _wa_number_or_raise(db, number_id)
        proxy_url = numero.proxy_url

    async with _open_wa_browser(number_id, headless=True, proxy_url=proxy_url) as context:
        page = await context.new_page()
        await page.goto(WHATSAPP_WEB_URL, wait_until="domcontentloaded")
        segnale = await WhatsAppWebPage(page).session_state()

    stato = stato_da_segnale(segnale)
    await _persist_status(number_id, stato)
    logger.info(f"check_session({number_id}): segnale={segnale} stato={stato.value}")
    return stato


async def assisted_login(number_id: str, timeout_s: int = 180) -> WaNumberStatus:
    """Login assistito: apre il browser VISIBILE (headless=False) sul
    profilo del numero perche' il QR si inquadra di persona (decisione
    27/07, niente pagina admin remota). Fa polling di session_state() finche'
    non vede 'logged_in' o scade il timeout.

    Se il profilo aveva gia' una sessione valida, il primo giro di polling
    la trova subito -- e' lo stesso path di check_session, solo headed
    perche' qui il chiamante si aspetta di dover guardare lo schermo.
    """
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        numero = await _wa_number_or_raise(db, number_id)
        proxy_url = numero.proxy_url

    segnale = "unknown"
    async with _open_wa_browser(number_id, headless=False, proxy_url=proxy_url) as context:
        page = await context.new_page()
        await page.goto(WHATSAPP_WEB_URL, wait_until="domcontentloaded")
        pom = WhatsAppWebPage(page)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            segnale = await pom.session_state()
            if segnale == "logged_in" or loop.time() >= deadline:
                break
            await asyncio.sleep(ASSISTED_LOGIN_POLL_INTERVAL_S)

    stato = stato_da_segnale(segnale)
    # da_lettura_automatica=False: questo e' un umano davanti allo schermo che
    # ha appena inquadrato un QR, non un cron che legge un DOM. E' l'atto
    # esplicito che puo' togliere un numero dal cooldown.
    await _persist_status(number_id, stato, da_lettura_automatica=False)
    logger.info(f"assisted_login({number_id}): segnale finale={segnale} stato={stato.value}")
    return stato
