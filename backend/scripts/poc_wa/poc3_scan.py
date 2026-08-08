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

# COMPILATO DAL CATALOGO il 27/07 (probe_riga + probe_riga2, DOM reale).
# I selettori precedenti erano ipotesi e sbagliavano quasi tutti:
#   [role='listitem']            -> NON ESISTE (le righe sono [role='row'])
#   span[aria-label*='non lett'] -> il badge e' [data-testid='icon-unread-count']
#   data-icon='status-*'         -> NON ESISTONO: le spunte sono <svg><title>
PANE_SEL = "#pane-side"
ROW_SEL = "[role='row']"
# Il titolo e' uno span[title] dentro cell-frame-title: l'attributo `title`
# porta il nome COMPLETO, l'innerText e' troncato con i puntini dal CSS.
TITLE_SEL = "[data-testid='cell-frame-title'] span[title], [data-testid='cell-frame-title'] span[dir='auto']"
UNREAD_SEL = "[data-testid='icon-unread-count']"
# Anche qui l'attributo `title` contiene l'anteprima INTERA, non troncata.
PREVIEW_SEL = "[data-testid='last-msg-status']"
MUTED_SEL = "[data-testid='mute-notifications-refreshed']"

JS_SCAN = """
(sels) => {
  const pane = document.querySelector(sels.pane);
  if (!pane) return {error: 'pane non trovato'};

  // WhatsApp inserisce marcatori di direzione del testo (U+202A/U+202C) dentro
  // i `title`. Restano invisibili a schermo ma sporcano confronti e ricerche —
  // e su console cp1252 hanno gia' ucciso uno script (27/07).
  const pulisci = (s) => (s || '').replace(/[\\u202a-\\u202e\\u2066-\\u2069]/g, '').trim();

  const rows = Array.from(pane.querySelectorAll(sels.row))
    // Le INTESTAZIONI di sezione sono [role='row'] come le chat: una riga vera
    // ha sempre un contenitore cell-frame (o e' la chat con se stessi).
    .filter(r => r.querySelector("[data-testid='cell-frame-title']")
              || r.matches("[data-testid='message-yourself-row']"));

  return rows.map((r, i) => {
    const t = r.querySelector(sels.title);
    const u = r.querySelector(sels.unread);
    const p = r.querySelector(sels.preview);

    // Direzione: se l'ultimo messaggio e' NOSTRO, WhatsApp disegna l'icona di
    // stato (inviato/consegnato/letto) come <svg> con un <title> 'wds-ic-*'.
    // Nessun data-icon: erano invisibili ai probe che filtravano per attributi.
    let statoUscita = null;
    for (const svg of r.querySelectorAll('svg')) {
      const ti = svg.querySelector('title');
      const nome = ti ? ti.textContent : '';
      if (nome && /^wds-ic-(read|delivered|sent|pending|check)/.test(nome)) {
        statoUscita = nome;
        break;
      }
    }

    return {
      position: i,
      title: pulisci(t ? (t.getAttribute('title') || t.innerText) : ''),
      unread_raw: u ? (u.innerText || u.getAttribute('aria-label') || '') : '',
      preview: pulisci(p ? (p.getAttribute('title') || p.innerText) : ''),
      last_is_outbound: statoUscita !== null,
      stato_uscita: statoUscita,          // wds-ic-read | wds-ic-delivered | ...
      muted: !!r.querySelector(sels.muted),
    };
  });
}
"""


# A8/H2/H3: cicli falliti di fila prima che il watcher si arrenda. L'esempio
# nel docstring usa --loop 15 (minuti): 5 cicli = ~75 minuti di cecita', abbastanza
# per assorbire un evaluate/reload singolo senza morire, ma non cosi' tanto da
# girare mezza giornata fingendo di raccogliere i 20 inbound del criterio GO
# mentre i selettori sono diventati stale o la pagina e' bloccata.
_MAX_CONSECUTIVE_FAILURES = 5


def _parse_unread(raw: str) -> int:
    digits = "".join(ch for ch in raw if ch.isdigit())
    return int(digits) if digits else (1 if raw else 0)


async def scan_once(page) -> list[dict]:
    rows = await page.evaluate(JS_SCAN, {
        "pane": PANE_SEL, "row": ROW_SEL, "title": TITLE_SEL,
        "unread": UNREAD_SEL, "preview": PREVIEW_SEL, "muted": MUTED_SEL,
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
            # NB: has_stop sull'anteprima NON e' una garanzia di opt-out — la
            # preview mostra solo l'ULTIMO messaggio, e uno STOP piu' vecchio
            # sparisce. Serve a scoprire i candidati; l'unica garanzia resta la
            # guardia pre-invio a chat aperta (poc2_send).
            "has_stop": contains_stop(r["preview"]),
            "last_is_outbound": r["last_is_outbound"],
            "stato_uscita": r["stato_uscita"],
            "muted": r["muted"],
        })
    return out


async def main(loop_minutes: int) -> None:
    consecutive_failures = 0
    async with wa_context(headless=False) as (context, page):
        # Si aspetta L'ELEMENTO, non un tempo. `wait_for_timeout(4000)` faceva
        # fallire il primo scan con "pane non trovato" su un profilo freddo, che
        # impiega 20s abbondanti a costruire l'app: stesso errore che aveva
        # ucciso poc1_login, in un punto diverso. Un numero magico piu' grande
        # non e' un fix, e' lo stesso bug rimandato.
        try:
            await page.wait_for_selector(PANE_SEL, timeout=90000)
        except Exception as e:
            await snap(page, "poc3-pane-mai-comparso")
            raise SystemExit(
                f"{PANE_SEL} non e' comparso in 90s: sessione scaduta, QR da rifare "
                f"o schermata inattesa. Guarda lo screenshot in artifacts/."
            ) from e

        while True:
            try:
                # scan_once solleva SystemExit se il pane non si aggancia: in
                # loop lo trattiamo come un ciclo fallito (potrebbe essere un
                # reload transitorio), non come la morte del watcher.
                rows = await scan_once(page)
            except (Exception, SystemExit) as e:
                consecutive_failures += 1
                await snap(page, f"poc3-scan-fallito-{consecutive_failures}")
                log_event("scan_failed", errore=f"{type(e).__name__}: {e}"[:200],
                          consecutive_failures=consecutive_failures)
                if not loop_minutes or consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    raise SystemExit(
                        f"Scan fallito {consecutive_failures} volte di fila: mi fermo "
                        f"invece di girare a vuoto. Guarda gli ultimi screenshot in artifacts/."
                    ) from e
                await page.wait_for_timeout(loop_minutes * 60 * 1000)
                continue

            consecutive_failures = 0
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
