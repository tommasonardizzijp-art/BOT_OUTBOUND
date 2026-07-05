"""Warm-up browser session: comportamento organico (feed scroll, post, like, storie)
via Patchright, da eseguire PRIMA delle fasi di scraping e DURANTE le pause lunghe.

Obiettivo: migliorare il rapporto attivita'-organica / attivita-automatica che il
risk-scoring notturno di Instagram misura per account. Un account che nello stesso
giorno fa SOLO chiamate API mobile (lista/bio via instagrapi) sembra 100% bot; se ha
anche una sessione browser vera sembra un utente reale che tra le altre cose usa un tool.

NON risolve il mismatch web-born/device-sintetico dell'API mobile: e' mitigazione a
livello trust dell'account, non una cura del canale API. Vedi memory
[[botoutbound-checkpoint-pattern-api]].

Vincolo di coerenza (IMPORTANTE): browser e API mobile devono uscire dallo STESSO
proxy per-account (impossible-travel altrimenti). Entrambi leggono `account.proxy`
(BrowserSession -> context_manager._fetch_account_proxy; instagrapi -> set_proxy),
quindi sono coerenti PER COSTRUZIONE finche' `account.proxy` e' impostato. Il chiamante
NON deve lanciare il warm-up mentre lo scraping API dello stesso account e' attivo:
va chiamato prima dell'avvio fase o durante una pausa (job API parcheggiato = idle).

Il modulo e' completamente difensivo: qualsiasi errore viene loggato e ingoiato, mai
sollevato, cosi' un warm-up fallito non ferma mai lo scraping.
"""
import asyncio
import random

from loguru import logger

from app.config import settings


async def run_warmup(
    account_id: str,
    username: str | None = None,
    min_minutes: float | None = None,
    max_minutes: float | None = None,
    headless: bool | None = None,
) -> dict:
    """Esegue una sessione di browsing organico per `account_id`.

    Riusa `BrowserSession` + `InstagramPage.browse_feed` (scroll feed, apri 0-2 post,
    like occasionale ~35% sessioni) — codice gia' in produzione nel flusso DM, qui
    solo orchestrato prima/durante lo scraping.

    Ritorna {"ran": bool, "duration_seconds": int, "reason": str} — mai solleva.
    """
    tag = f"@{username}" if username else account_id[:8] + "…"

    if not settings.warmup_browse_enabled:
        return {"ran": False, "duration_seconds": 0, "reason": "disabled"}

    lo = settings.warmup_browse_min_minutes if min_minutes is None else min_minutes
    hi = settings.warmup_browse_max_minutes if max_minutes is None else max_minutes
    if hi < lo:
        lo, hi = hi, lo
    duration_s = int(random.uniform(lo, hi) * 60)
    use_headless = settings.warmup_browse_headless if headless is None else headless

    # Import ritardato: patchright/context_manager sono pesanti e non sempre presenti
    # negli ambienti di test unit del backend.
    try:
        from app.browser.context_manager import BrowserSession
    except Exception as e:  # pragma: no cover
        logger.warning(f"[Warmup] {tag}: BrowserSession non importabile ({e}) — skip")
        return {"ran": False, "duration_seconds": 0, "reason": f"import_error:{e}"}

    logger.info(f"[Warmup] {tag}: avvio sessione organica ~{duration_s}s (headless={use_headless})")

    session = BrowserSession(account_id, headless=use_headless)
    try:
        await session.open()
        page_obj = session.page  # InstagramPage
        # Verifica login (naviga a instagram.com e controlla la sessione del profilo).
        await page_obj.ensure_logged_in(account_id)
        # Browsing ambientale sul feed: scroll variato, pause lettura, 0-2 post aperti,
        # like raro. Non solleva mai (difensivo di suo).
        await page_obj.browse_feed(duration_s)
        logger.info(f"[Warmup] {tag}: sessione organica completata ({duration_s}s)")
        return {"ran": True, "duration_seconds": duration_s, "reason": "ok"}
    except Exception as e:
        logger.warning(f"[Warmup] {tag}: sessione fallita ({type(e).__name__}: {e}) — ingoiato, scraping prosegue")
        return {"ran": False, "duration_seconds": 0, "reason": f"error:{type(e).__name__}"}
    finally:
        try:
            await session.close()
        except Exception as e:  # pragma: no cover
            logger.debug(f"[Warmup] {tag}: close fallita ({e})")


async def run_warmup_safe(account_id: str, username: str | None = None, **kw) -> None:
    """Come run_warmup ma senza valore di ritorno e con guardia extra: pensata per
    essere chiamata inline dai worker scraping senza rischio di propagare eccezioni."""
    try:
        await run_warmup(account_id, username, **kw)
    except Exception as e:  # cintura + bretelle
        logger.warning(f"[Warmup] guardia esterna: {type(e).__name__}: {e}")
