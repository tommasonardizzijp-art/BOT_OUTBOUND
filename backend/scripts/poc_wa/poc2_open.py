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

from _common import (apri_chat_da_risultati, artifacts_dir, first_locator, human_type,
                     log_event, snap, svuota_ricerca, wa_context)
from wa_lib import AllowList, normalize_e164

# ALLINEATI AL CATALOGO il 27/07. I precedenti erano ipotesi:
# [data-testid='chat-list-search'] non esiste, data-tab='3'/'10' non verificati.
SEARCH_SEL = ["[role='textbox']", "[data-testid='chat-list-search']",
              "div[aria-label*='Cerca']", "div[aria-label*='Search']"]
# WhatsApp Web usa ora l'editor Lexical; l'aria-label del composer e'
# "Digita un messaggio per <nome>", non "Scrivi un messaggio".
COMPOSER_SEL = ["[contenteditable='true'][data-lexical-editor='true']",
                "div[aria-label^='Digita un messaggio']",
                "div[aria-label*='Type a message']",
                "footer [contenteditable='true']"]

# Cronologia = messaggi nel PANNELLO CONVERSAZIONE (guardia V2).
# ATTENZIONE, bug corretto il 27/07: la lista precedente conteneva
# `[role='row']` SENZA SCOPE. Nella sidebar le righe ci sono sempre, quindi il
# segnale sarebbe stato "ha cronologia" SEMPRE — anche su una chat inesistente,
# che e' esattamente il caso che questa guardia deve intercettare. Tutti i
# selettori sono ora ancorati a #main, e div.message-in/out sono stati tolti
# perche' misurati a 0 (non esistono piu').
HISTORY_SEL = ["#main [data-id]", "#main [data-testid^='conv-msg-']",
               "#main [data-testid='msg-container']"]
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

    # Svuota PRIMA di digitare: senza, il numero si accoda a quello del giro
    # precedente e la ricerca cerca una stringa inesistente (27/07).
    if not await svuota_ricerca(page, box[0]):
        await snap(page, "poc2-open-ricerca-non-svuotata")
        return False, (time.perf_counter() - t0) * 1000, "nessuna-cronologia:ricerca-non-svuotata"
    # human_type e NON keyboard.type(delay=60): un ritardo fisso e' varianza
    # ZERO su dodici cifre consecutive, la firma robotica piu' banale da
    # misurare. Coerente con la regola gia' adottata su Instagram: struttura e
    # header rigidi, timing rumoroso. (Corretto il 27/07 su domanda di Tommaso.)
    await human_type(page, box[0], e164)
    await page.wait_for_timeout(2500)
    # A4: verifica che il focus sia ANCORA sulla casella di ricerca. In
    # --strategia both questa funzione gira appena dopo il deep-link, che ha
    # gia' aperto una chat col suo composer: se il focus e' finito li', un
    # tasto qualsiasi finirebbe dentro un messaggio da uno script che dichiara
    # "zero invii". Se il focus non e' verificabile, si abortisce.
    focused = await box[0].evaluate("el => el === document.activeElement")
    if not focused:
        await snap(page, "poc2-open-search-focus-perso")
        ms = (time.perf_counter() - t0) * 1000
        return False, ms, "nessuna-cronologia:focus-non-sulla-ricerca-pre-invio"

    # Selezione PER SEZIONE invece di Enter (corretto il 27/07). Enter apre il
    # primo risultato: se il contatto non ha una chat 1:1, quel risultato e' un
    # GRUPPO — fuori perimetro, e aprirlo lo marca pure come letto.
    aperto, nota = await apri_chat_da_risultati(page)
    if not aperto:
        ms = (time.perf_counter() - t0) * 1000
        return False, ms, f"nessuna-cronologia:{nota}"
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
