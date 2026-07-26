# backend/scripts/poc_wa/poc2_send.py
"""PoC-2b — guardia pre-invio + invio reale + verifica spunte.

Sequenza per ogni destinatario (identica a quella che il sender di M3 dovra' fare):
  1. apre la chat con la strategia vincente di PoC-2a;
  2. GUARDIA PRE-INVIO: legge i messaggi inbound successivi all'ultimo messaggio
     nostro e cerca uno STOP -> cronometrata, target <= 2s (SDD 13, PoC-2);
  3. se STOP -> NON invia, registra e passa oltre;
  4. altrimenti digita in modo umano e invia;
  5. legge la spunta dell'ultimo messaggio inviato (orologio/1/2) -> Q39.

TRE GUARDIE prima di scrivere a qualcuno:
  - allowlist fail-closed (POC_WA_ALLOWED_NUMBERS);
  - flag --send esplicito (senza, e' dry-run: apre, misura la guardia, non invia);
  - STOP trovato = stop assoluto.

Uso:  python poc2_send.py --numero "+39..." --messaggio-file D:\\wa-poc\\messages.txt [--send]
"""
import argparse
import asyncio
import csv
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from _common import artifacts_dir, first_locator, human_type, log_event, snap, wa_context
from poc2_open import COMPOSER_SEL, open_by_deeplink
from wa_lib import AllowList, contains_stop, mask_pii, normalize_e164

# Legge la coda di messaggi in fondo alla conversazione: gli inbound dopo
# l'ultimo outbound nostro. Budget fisso: cio' che e' renderizzato, niente scroll
# infinito (SDD: budget = visibili + 1-2 scroll).
# ATTENZIONE — selettori NON ancora verificati sul DOM reale (lo fa il Task 5,
# step 1b). Se 'div.message-in'/'div.message-out' non agganciano nulla, questa
# funzione torna una lista VUOTA: la guardia concluderebbe "nessuno STOP" e
# invierebbe SEMPRE, sembrando funzionare. Prima di usare questo script con
# --send, controlla nel catalogo (docs/whatsapp/wa-dom-catalog.md) che i
# conteggi delle bolle siano > 0.
JS_TAIL = """
() => {
  const rows = Array.from(document.querySelectorAll('div.message-in, div.message-out'));
  const tail = [];
  for (let i = rows.length - 1; i >= 0 && tail.length < 30; i--) {
    const el = rows[i];
    const isOut = el.classList.contains('message-out');
    if (isOut) break;                     // fermati al nostro ultimo messaggio
    tail.push((el.innerText || '').slice(0, 300));
  }
  return tail.reverse();
}
"""
TICK_SEL = ["[data-icon='status-dblcheck']", "[data-icon='status-check']", "[data-icon='status-time']"]


async def guardia_pre_invio(page) -> tuple[list[str], float, bool]:
    """Ritorna (inbound_dopo_ultimo_nostro, millisecondi, stop_trovato)."""
    t0 = time.perf_counter()
    tail = await page.evaluate(JS_TAIL)
    ms = (time.perf_counter() - t0) * 1000
    stop = any(contains_stop(t) for t in tail)
    return tail, ms, stop


async def leggi_spunta(page) -> str:
    found = await first_locator(page, TICK_SEL, timeout_ms=8000)
    return found[1] if found else "nessuna-spunta-letta"


async def main(numero: str, messaggio: str, send: bool) -> None:
    allow = AllowList.load()
    e164 = normalize_e164(numero)
    if not e164:
        raise SystemExit(f"Numero non normalizzabile: {numero!r}")
    allow.assert_allowed(e164)   # fail-closed: solleva se non e' una chat controllata

    path = artifacts_dir() / "send_results.csv"
    new = not path.exists()
    async with wa_context(headless=False) as (context, page):
        t_start = time.perf_counter()
        ok, open_ms, segnale = await open_by_deeplink(page, e164)
        if not ok:
            await snap(page, "poc2-send-apertura-fallita")
            raise SystemExit(f"Chat non aperta ({segnale}): niente invio.")
        if "nessuna-cronologia" in segnale:
            raise SystemExit("Chat senza cronologia: V2 vieta di scrivere. Stop.")

        tail, guardia_ms, stop = await guardia_pre_invio(page)
        inviato, spunta, note = False, "", ""
        if stop:
            note = "STOP trovato nella coda inbound: invio annullato"
        elif not send:
            note = "dry-run (nessun --send)"
        else:
            comp = await first_locator(page, COMPOSER_SEL, timeout_ms=10000)
            if not comp:
                await snap(page, "poc2-composer-non-trovato")
                raise SystemExit("Composer non trovato: catalogare il selettore prima di riprovare.")
            await human_type(page, comp[0], messaggio)
            await asyncio.sleep(random.uniform(0.4, 1.2))
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(2500)
            spunta = await leggi_spunta(page)
            inviato = True

        totale_ms = (time.perf_counter() - t_start) * 1000
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["ts", "numero_masked", "guardia_ms", "inbound_letti", "stop_trovato",
                            "inviato", "spunta", "totale_ms", "note"])
            w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), f"…{e164[-4:]}",
                        round(guardia_ms), len(tail), int(stop), int(inviato), spunta,
                        round(totale_ms), note])
        log_event("send_attempt", guardia_ms=round(guardia_ms), inbound=len(tail), stop=stop,
                  inviato=inviato, spunta=spunta, totale_ms=round(totale_ms),
                  ultimo_inbound=mask_pii(tail[-1] if tail else "", keep=60))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--numero", required=True)
    ap.add_argument("--messaggio-file", required=True, help="file con i messaggi veri scritti da Tommaso")
    ap.add_argument("--send", action="store_true", help="senza questo flag e' dry-run")
    args = ap.parse_args()
    righe = [r.strip() for r in Path(args.messaggio_file).read_text(encoding="utf-8").splitlines() if r.strip()]
    if not righe:
        raise SystemExit("File messaggi vuoto: scrivi i testi veri prima (Task 0 step 6).")
    asyncio.run(main(args.numero, random.choice(righe), args.send))
