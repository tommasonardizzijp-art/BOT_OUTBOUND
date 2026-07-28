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
# Rilevato in review il 28/07, da affrontare nella chiusura del modulo.
ASSISTED_LOGIN_POLL_INTERVAL_S = 2.0


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
    con un account_id che avesse lo stesso UUID."""
    from app.config import settings

    return Path(settings.browser_profiles_dir) / f"wa_{number_id}"


def _get_wa_lock(number_id: str) -> asyncio.Lock:
    """Lock per-numero: stessa forma di _get_account_lock (app/browser/
    context_manager.py:32), chiave namespaced wa_<number_id> per condividere
    il dizionario dei lock senza rischiare collisioni con gli account_id
    Instagram."""
    from app.browser.context_manager import _get_account_lock

    return _get_account_lock(f"wa_{number_id}")


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
    try:
        from patchright.async_api import async_playwright
    except ImportError:
        raise ImportError(
            "Patchright is not installed. Run: pip install patchright && patchright install chromium"
        )

    from app.browser.context_manager import _build_fingerprint_script, parse_proxy_url
    from app.browser.fingerprint import get_fingerprint

    profile_dir = profile_dir_for(number_id)
    profile_dir.mkdir(parents=True, exist_ok=True)

    # Lock file di Chromium lasciati da una sessione precedente crashata/killata
    # (stessa causa in context_manager._prepare_launch): senza questa pulizia
    # Chrome vede "sessione esistente", inoltra il launch al PID fantasma ed
    # esce subito -- TargetClosedError prima che Patchright si connetta.
    for lock_file in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lock_path = profile_dir / lock_file
        try:
            if lock_path.exists():
                os.remove(lock_path)
        except OSError as e:
            logger.warning(f"wa_session: impossibile rimuovere {lock_file} per {number_id}: {e}")

    fingerprint = get_fingerprint(number_id)
    proxy_cfg = parse_proxy_url(proxy_url)
    if proxy_url and not proxy_cfg:
        raise ValueError(f"Proxy malformato per numero WA {number_id}: {proxy_url!r}")

    chromium_args = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
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


async def _persist_status(number_id: str, stato: WaNumberStatus) -> None:
    from datetime import datetime

    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        numero = await _wa_number_or_raise(db, number_id)
        numero.status = stato
        numero.session_checked_at = datetime.utcnow()
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
    await _persist_status(number_id, stato)
    logger.info(f"assisted_login({number_id}): segnale finale={segnale} stato={stato.value}")
    return stato
