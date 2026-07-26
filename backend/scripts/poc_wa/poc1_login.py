# backend/scripts/poc_wa/poc1_login.py
"""PoC-1a — login iniziale via QR sul numero secondario Primero.

Da eseguire UNA VOLTA, con il telefono in mano. Da qui parte il cronometro dei
14 giorni: ogni re-scan richiesto dopo questo momento e' un dato di PoC-1.

Uso:  python poc1_login.py
"""
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from _common import artifacts_dir, first_locator, log_event, snap, wa_context

# Candidati per "sono loggato": la lista chat e' visibile.
CHATLIST_CANDIDATES = [
    "#pane-side",
    "[data-testid='chat-list']",
    "div[aria-label*='Elenco chat']",
    "div[aria-label*='Chat list']",
    "[role='grid']",
]
# Candidati per "serve il QR".
QR_CANDIDATES = [
    "canvas[aria-label*='Scan']",
    "[data-testid='qrcode']",
    "canvas",
]


async def main() -> None:
    async with wa_context(headless=False) as (context, page):
        found = await first_locator(page, CHATLIST_CANDIDATES, timeout_ms=8000)
        if found:
            _, sel = found
            log_event("already_logged_in", selector=sel)
            print("Sessione gia' attiva: nessun QR necessario.")
        else:
            qr = await first_locator(page, QR_CANDIDATES, timeout_ms=15000)
            if not qr:
                await snap(page, "poc1-schermata-ignota")
                log_event("login_unknown_screen")
                raise SystemExit(
                    "Ne' lista chat ne' QR: schermata non prevista. "
                    "Guarda lo screenshot in artifacts/ e catalogala."
                )
            _, qr_sel = qr
            log_event("qr_shown", selector=qr_sel)
            print("Inquadra il QR col telefono (Dispositivi collegati). Attendo fino a 3 minuti…")
            got = await first_locator(page, CHATLIST_CANDIDATES, timeout_ms=180000)
            if not got:
                await snap(page, "poc1-login-fallito")
                raise SystemExit("Login non completato entro 3 minuti.")
            _, sel = got
            log_event("login_ok", selector=sel)

        # Marker: da qui si contano i giorni di sessione viva.
        marker = artifacts_dir() / "session_start.txt"
        if not marker.exists():
            marker.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
        await snap(page, "poc1-logged-in")
        log_event("session_established", marker=str(marker))
        print("OK. Non chiudere il profilo a mano: da ora gira poc1_heartbeat.py ogni giorno.")


if __name__ == "__main__":
    asyncio.run(main())
