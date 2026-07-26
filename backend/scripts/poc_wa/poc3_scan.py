# backend/scripts/poc_wa/poc3_scan.py
"""PoC-3b — rilevamento inbound dalla SOLA lista chat.

Sostituire i selettori sotto con quelli catalogati nel Task 5 (wa-dom-catalog.md).
Regola non negoziabile: nessun click su una riga chat. Se serve un click per
capire qualcosa, PoC-3 e' NO-GO e va scritto nel report.

Uso:  python poc3_scan.py            # uno scan
      python poc3_scan.py --loop 15  # uno scan ogni 15 minuti (simula il watcher)
"""
import argparse
import asyncio
import json
from datetime import datetime

from _common import artifacts_dir, log_event, snap, wa_context
from wa_lib import contains_stop, mask_pii

# <<< DA COMPILARE DAL CATALOGO (Task 5) >>>
PANE_SEL = "#pane-side"
ROW_SEL = "[role='listitem']"
TITLE_SEL = "span[title]"
UNREAD_SEL = "span[aria-label*='non lett']"
PREVIEW_SEL = "[data-testid='last-msg-status'], span[dir='ltr']"
OUTBOUND_ICON_SEL = "[data-icon='status-dblcheck'], [data-icon='status-check'], [data-icon='status-time']"

JS_SCAN = """
(sels) => {
  const pane = document.querySelector(sels.pane);
  if (!pane) return {error: 'pane non trovato'};
  const rows = Array.from(pane.querySelectorAll(sels.row));
  return rows.map((r, i) => {
    const t = r.querySelector(sels.title);
    const u = r.querySelector(sels.unread);
    const p = r.querySelector(sels.preview);
    const o = r.querySelector(sels.outIcon);
    return {
      position: i,
      title: t ? (t.getAttribute('title') || t.innerText || '') : '',
      unread_raw: u ? (u.getAttribute('aria-label') || u.innerText || '') : '',
      preview: p ? (p.innerText || '') : '',
      last_is_outbound: !!o,
    };
  });
}
"""


def _parse_unread(raw: str) -> int:
    digits = "".join(ch for ch in raw if ch.isdigit())
    return int(digits) if digits else (1 if raw else 0)


async def scan_once(page) -> list[dict]:
    rows = await page.evaluate(JS_SCAN, {
        "pane": PANE_SEL, "row": ROW_SEL, "title": TITLE_SEL,
        "unread": UNREAD_SEL, "preview": PREVIEW_SEL, "outIcon": OUTBOUND_ICON_SEL,
    })
    if isinstance(rows, dict) and rows.get("error"):
        raise SystemExit(f"Scan fallito: {rows['error']} — ricontrolla i selettori del catalogo.")
    out = []
    for r in rows:
        out.append({
            "position": r["position"],
            "title_masked": mask_pii(r["title"], keep=40),
            "title_is_number": r["title"].replace(" ", "").replace("+", "").isdigit(),
            "unread_count": _parse_unread(r["unread_raw"]),
            "preview_masked": mask_pii(r["preview"], keep=60),
            "has_stop": contains_stop(r["preview"]),
            "last_is_outbound": r["last_is_outbound"],
        })
    return out


async def main(loop_minutes: int) -> None:
    async with wa_context(headless=False) as (context, page):
        while True:
            await page.wait_for_timeout(4000)
            rows = await scan_once(page)
            unread = [r for r in rows if r["unread_count"] > 0]
            path = artifacts_dir() / f"scan_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
            path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            log_event("scan_done", righe=len(rows), non_letti=len(unread),
                      titoli_numerici=sum(1 for r in rows if r["title_is_number"]),
                      stop_visti=sum(1 for r in rows if r["has_stop"]))
            if not loop_minutes:
                return
            await page.wait_for_timeout(loop_minutes * 60 * 1000)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0, help="minuti tra uno scan e l'altro")
    args = ap.parse_args()
    asyncio.run(main(args.loop))
