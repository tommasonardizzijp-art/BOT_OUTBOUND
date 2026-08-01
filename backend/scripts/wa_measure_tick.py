"""Misura quanto ci mette la spunta a comparire dopo un invio reale.

Usa-e-getta come gli script di M0: serve a produrre UN numero con la sua
provenienza, non a restare in produzione.

NON usare il profilo di PoC-1 (D:\\dev\\wa-poc\\profile): un re-scan del QR
azzera 14 giorni di misura. NON impostare PLAYWRIGHT_BROWSERS_PATH.
"""
import asyncio
import statistics
import sys
import time

from app.browser.whatsapp_page import WhatsAppWebPage
from app.services.wa_session import _open_wa_browser, WHATSAPP_WEB_URL


async def main(number_id: str, e164: str, testo: str, ripetizioni: int = 5):
    misure = []
    async with _open_wa_browser(number_id, headless=False, proxy_url=None) as context:
        page = await context.new_page()
        await page.goto(WHATSAPP_WEB_URL, wait_until="domcontentloaded")
        pom = WhatsAppWebPage(page)
        for i in range(ripetizioni):
            res = await pom.open_chat(e164)
            if not res.ok:
                print(f"[{i}] apertura fallita: {res.signal}")
                continue
            await pom.send_text(f"{testo} ({i + 1})")
            t0 = time.perf_counter()
            spunta = "nessuna-spunta-letta"
            while (time.perf_counter() - t0) < 60:
                spunta = await pom.read_last_tick()
                if spunta != "nessuna-spunta-letta":
                    break
                await asyncio.sleep(0.5)
            ms = (time.perf_counter() - t0) * 1000
            misure.append(ms)
            print(f"[{i}] spunta={spunta!r} dopo {round(ms)} ms")
            await asyncio.sleep(30)
    if misure:
        print(f"n={len(misure)} mediana={round(statistics.median(misure))} ms "
              f"max={round(max(misure))} ms")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3]))
