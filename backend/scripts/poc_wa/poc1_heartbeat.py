# backend/scripts/poc_wa/poc1_heartbeat.py
"""PoC-1b — la sessione e' ancora viva? e quanto costa tenerla aperta?

Da lanciare almeno 1 volta al giorno per 14 giorni (Task 4, step 2). Ogni run:
apre il profilo, guarda se c'e' la lista chat o il QR, campiona RAM/CPU dei
processi Chromium di QUESTO profilo, scrive una riga nel CSV, chiude.

Il criterio GO di PoC-1: 14 giorni senza re-scan, >= 5 riavvii browser e >= 2
riavvii PC sopportati. Ogni riga di questo CSV e' un riavvio browser.

Uso:  python poc1_heartbeat.py [--nota "riavviato il PC"]
"""
import argparse
import asyncio
import csv
from datetime import datetime, timezone

import psutil

from _common import artifacts_dir, cmdline_matches_profile, first_locator, log_event, snap, wa_context
from poc1_login import (
    CHATLIST_CANDIDATES,
    QR_CANDIDATES,
    TIMEOUT_CHATLIST_MS,
    TIMEOUT_QR_MS,
)
from wa_lib import mask_pii

CSV_PATH = None  # impostato in main()


def _sample_profile_processes() -> tuple[float, float]:
    """RSS totale (MB) e CPU% dei processi Chromium legati a QUESTO profilo.

    Il match e' sull'argomento --user-data-dir=<path> (cmdline_matches_profile),
    non su una substring libera della cmdline: evita sia di contare il Chrome
    personale di Tommaso sia i falsi positivi con profili dal nome simile.
    """
    rss = 0
    cpu = 0.0
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if cmdline_matches_profile(proc.info.get("cmdline")):
                rss += proc.memory_info().rss
                cpu += proc.cpu_percent(interval=0.1)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return round(rss / (1024 * 1024), 1), round(cpu, 1)


async def main(nota: str) -> None:
    global CSV_PATH
    CSV_PATH = artifacts_dir() / "heartbeat.csv"
    marker = artifacts_dir() / "session_start.txt"
    start = datetime.fromisoformat(marker.read_text(encoding="utf-8")) if marker.exists() else None

    async with wa_context(headless=False) as (context, page):
        # Timeout dalle costanti di poc1_login, NON numeri scritti qui: prima
        # erano 20000/5000 a mano, e il 28/07 hanno prodotto un falso
        # "SESSIONE PERSA" -- #pane-side aveva agganciato dopo 19820 ms,
        # centottanta millisecondi oltre il limite. Su un PoC il cui criterio
        # e' "14 giorni senza re-scan" un falso negativo non e' un fastidio:
        # sporca il CSV che decide il verdetto, e chi legge "SESSIONE PERSA"
        # rifa' il QR, azzerando per davvero la misura che il test stava
        # superando.
        alive = await first_locator(page, CHATLIST_CANDIDATES, timeout_ms=TIMEOUT_CHATLIST_MS)
        if alive:
            viva, sel = True, alive[1]
        else:
            qr = await first_locator(page, QR_CANDIDATES, timeout_ms=TIMEOUT_QR_MS)
            viva, sel = False, (qr[1] if qr else "schermata-ignota")
            await snap(page, "poc1-sessione-persa")
        rss_mb, cpu_pct = _sample_profile_processes()
        giorni = (datetime.now(timezone.utc) - start).days if start else -1

        new_file = not CSV_PATH.exists()
        with CSV_PATH.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(["ts", "giorni_da_login", "sessione_viva", "selettore", "rss_mb", "cpu_pct", "note"])
            w.writerow([
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                giorni, int(viva), sel, rss_mb, cpu_pct, mask_pii(nota),
            ])
        log_event("heartbeat", giorni=giorni, viva=viva, rss_mb=rss_mb, cpu_pct=cpu_pct, nota=nota)
        if not viva:
            print("!! SESSIONE PERSA — annota cosa e' successo prima (aggiornamenti, riavvii, telefono offline).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--nota", default="")
    args = ap.parse_args()
    asyncio.run(main(args.nota))
