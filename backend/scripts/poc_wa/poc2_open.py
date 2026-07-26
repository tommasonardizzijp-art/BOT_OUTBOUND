# backend/scripts/poc_wa/poc2_open.py
"""PoC-2a — si apre una chat esistente PER NUMERO in modo deterministico?

Confronta due strategie:
  A) deep-link  https://web.whatsapp.com/send?phone=<e164>
  B) ricerca interna nella lista chat

Misura: successo/fallimento, millisecondi, e che segnale distingue
"chat con cronologia" da "chat inesistente" (serve alla guardia V2 dell'SDD).

Questo script NON invia: non tocca il composer. L'allowlist protegge comunque
dal caso "deep-link a un numero sbagliato apre una chat nuova con un cliente".

Uso:  python poc2_open.py --numeri "+39...,+39..." --strategia deeplink|search|both
"""
import argparse
import asyncio
import csv
import time
from datetime import datetime, timezone

from _common import artifacts_dir, first_locator, log_event, snap, wa_context
from wa_lib import AllowList, normalize_e164

SEARCH_SEL = ["[data-testid='chat-list-search']", "div[contenteditable='true'][data-tab='3']",
              "div[aria-label*='Cerca']", "div[aria-label*='Search']"]
COMPOSER_SEL = ["div[contenteditable='true'][data-tab='10']", "div[aria-label*='Scrivi un messaggio']",
                "div[aria-label*='Type a message']", "footer div[contenteditable='true']"]
# Messaggi gia' presenti nella conversazione = la chat ha cronologia (guardia V2).
HISTORY_SEL = ["div.message-in", "div.message-out", "[data-testid='msg-container']", "[role='row']"]
NO_CHAT_SEL = ["text=Il numero di telefono condiviso tramite url non è valido",
               "text=Phone number shared via url is invalid", "[data-testid='popup-contents']"]


async def _history_signal(page) -> str:
    found = await first_locator(page, HISTORY_SEL, timeout_ms=5000)
    if found:
        count = await page.locator(found[1]).count()
        return f"cronologia:{found[1]}:{count}"
    missing = await first_locator(page, NO_CHAT_SEL, timeout_ms=2000)
    return f"nessuna-cronologia:{missing[1] if missing else 'nessun-segnale'}"


async def open_by_deeplink(page, e164: str) -> tuple[bool, float, str]:
    t0 = time.perf_counter()
    await page.goto(f"https://web.whatsapp.com/send?phone={e164}", wait_until="domcontentloaded", timeout=60000)
    ok = await first_locator(page, COMPOSER_SEL, timeout_ms=25000)
    ms = (time.perf_counter() - t0) * 1000
    return bool(ok), ms, await _history_signal(page)


async def open_by_search(page, e164: str) -> tuple[bool, float, str]:
    t0 = time.perf_counter()
    box = await first_locator(page, SEARCH_SEL, timeout_ms=10000)
    if not box:
        # A3: il ramo di fallimento deve contenere il marcatore 'nessuna-cronologia',
        # altrimenti il Task 8 lo legge come "ha cronologia, procedi".
        return False, (time.perf_counter() - t0) * 1000, "nessuna-cronologia:casella-ricerca-non-trovata"
    await box[0].click()
    await page.keyboard.type(e164, delay=60)
    await page.wait_for_timeout(2500)
    # A4: verifica che il focus sia ANCORA sulla casella di ricerca subito prima
    # di premere Enter. In --strategia both questa funzione gira appena dopo il
    # deep-link, che ha gia' aperto una chat col suo composer: se il focus e'
    # finito li' (per qualunque motivo), quell'Enter invia un messaggio vero da
    # uno script che dichiara "zero invii". Se il focus non torna verificabile,
    # si abortisce invece di premere.
    focused = await box[0].evaluate("el => el === document.activeElement")
    if not focused:
        await snap(page, "poc2-open-search-focus-perso")
        ms = (time.perf_counter() - t0) * 1000
        return False, ms, "nessuna-cronologia:focus-non-sulla-ricerca-pre-invio"
    await page.keyboard.press("Enter")
    ok = await first_locator(page, COMPOSER_SEL, timeout_ms=15000)
    ms = (time.perf_counter() - t0) * 1000
    return bool(ok), ms, await _history_signal(page)


async def main(numeri: list[str], strategia: str) -> None:
    allow = AllowList.load()
    if not len(allow):
        raise SystemExit("POC_WA_ALLOWED_NUMBERS non configurata: mi fermo (fail-closed).")

    path = artifacts_dir() / "open_results.csv"
    new = not path.exists()
    async with wa_context(headless=False) as (context, page):
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["ts", "strategia", "numero_masked", "esito", "ms", "segnale_cronologia", "note"])
            for raw in numeri:
                e164 = normalize_e164(raw)
                if not e164:
                    print(f"scarto: {raw!r} non normalizzabile")
                    continue
                allow.assert_allowed(e164)  # anche in sola apertura: niente sorprese
                for strat in (["deeplink", "search"] if strategia == "both" else [strategia]):
                    fn = open_by_deeplink if strat == "deeplink" else open_by_search
                    try:
                        ok, ms, segnale = await fn(page, e164)
                        note = ""
                    except Exception as e:
                        # A3: idem, l'eccezione non deve leggersi come "ha cronologia".
                        ok, ms, segnale = False, -1, "nessuna-cronologia:eccezione"
                        note = f"{type(e).__name__}: {e}"[:160]
                        await snap(page, f"poc2-open-fail-{strat}")
                    w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), strat,
                                f"…{e164[-4:]}", "OK" if ok else "KO", round(ms), segnale, note])
                    log_event("open_chat", strategia=strat, esito=ok, ms=round(ms), segnale=segnale)
                    await page.wait_for_timeout(3000)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--numeri", required=True, help="numeri separati da virgola (solo chat controllate)")
    ap.add_argument("--strategia", default="both", choices=["deeplink", "search", "both"])
    args = ap.parse_args()
    asyncio.run(main([n for n in args.numeri.split(",") if n.strip()], args.strategia))
