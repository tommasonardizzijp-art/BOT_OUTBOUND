# backend/scripts/poc_wa/poc3_dump_dom.py
"""PoC-3a — che informazione espone la lista chat, senza aprire nulla?

Non assume selettori: cammina il pannello laterale e raccoglie, per ogni riga
candidata, gli attributi utili (role, aria-label, data-*, testi). Il risultato
serve a compilare docs/whatsapp/wa-dom-catalog.md, che sara' la base del POM in M1.

I testi vengono mascherati e troncati: qui dentro ci sono conversazioni vere di
clienti di Primero.

Uso:  python poc3_dump_dom.py
      python poc3_dump_dom.py --chat "+39..."   # dump del pannello conversazione (Step 1b)
"""
import argparse
import asyncio
import json
from datetime import datetime

from _common import artifacts_dir, log_event, snap, wa_context
from wa_lib import AllowList, mask_pii, normalize_e164

PANE_CANDIDATES = ["#pane-side", "[data-testid='chat-list']", "div[aria-label*='Elenco chat']", "[role='grid']"]

# Cammina il pannello e descrive i primi N nodi "riga" plausibili.
JS_DUMP = """
(paneSel) => {
  const pane = document.querySelector(paneSel);
  if (!pane) return {error: 'pane non trovato', sel: paneSel};
  const rows = Array.from(pane.querySelectorAll("[role='listitem'], [role='row'], [data-testid='cell-frame-container']"));
  const describe = (el) => {
    const attrs = {};
    for (const a of el.attributes) {
      if (a.name.startsWith('data-') || a.name.startsWith('aria-') || a.name === 'role' || a.name === 'title') {
        attrs[a.name] = a.value;
      }
    }
    return {tag: el.tagName.toLowerCase(), attrs, text: (el.innerText || '').slice(0, 200)};
  };
  return {
    sel: paneSel,
    rowCount: rows.length,
    rows: rows.slice(0, 12).map(r => ({
      self: describe(r),
      children: Array.from(r.querySelectorAll('span[title], span[aria-label], [data-icon], [aria-label]'))
                     .slice(0, 25).map(describe),
    })),
  };
}
"""

# Descrive le ultime bolle di messaggio e le icone di stato: da qui nascono
# JS_TAIL, TICK_SEL e HISTORY_SEL del Task 8. `class` e' inclusa di proposito:
# 'message-in'/'message-out' sono classi, non data-attribute.
JS_DUMP_CONV = """
() => {
  const describe = (el) => {
    const attrs = {};
    for (const a of el.attributes) {
      if (a.name.startsWith('data-') || a.name.startsWith('aria-')
          || a.name === 'role' || a.name === 'class' || a.name === 'title') {
        attrs[a.name] = a.value;
      }
    }
    return {tag: el.tagName.toLowerCase(), attrs, text: (el.innerText || '').slice(0, 120)};
  };
  const main = document.querySelector('#main')
            || document.querySelector("[role='application']")
            || document.body;
  const bubbles = Array.from(main.querySelectorAll(
      "div.message-in, div.message-out, [data-id], [role='row']")).slice(-14);
  return {
    mainSel: main.id ? '#' + main.id : main.tagName.toLowerCase(),
    counts: {
      'div.message-in': main.querySelectorAll('div.message-in').length,
      'div.message-out': main.querySelectorAll('div.message-out').length,
      '[data-id]': main.querySelectorAll('[data-id]').length,
      "[role='row']": main.querySelectorAll("[role='row']").length,
    },
    bubbles: bubbles.map(describe),
    dataIcons: Array.from(main.querySelectorAll('[data-icon]'))
                    .slice(-30).map(e => e.getAttribute('data-icon')),
    composers: Array.from(main.querySelectorAll("div[contenteditable='true']")).map(describe),
  };
}
"""


async def dump_conversazione(page, raw_numero: str) -> dict:
    """Apre UNA chat controllata e descrive il pannello conversazione."""
    e164 = normalize_e164(raw_numero)
    if not e164:
        raise SystemExit(f"Numero non normalizzabile: {raw_numero!r}")
    AllowList.load().assert_allowed(e164)   # fail-closed anche in sola lettura
    await page.goto(f"https://web.whatsapp.com/send?phone={e164}",
                    wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(8000)
    dump = await page.evaluate(JS_DUMP_CONV)
    for b in dump["bubbles"] + dump["composers"]:
        b["text"] = mask_pii(b.get("text", ""), keep=60)
        for k in list(b.get("attrs", {})):
            b["attrs"][k] = mask_pii(b["attrs"][k], keep=80)
    out = artifacts_dir() / f"conv_dump_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
    log_event("conv_dump", main=dump["mainSel"], counts=json.dumps(dump["counts"]),
              icone=",".join(sorted(set(dump["dataIcons"]))), file=str(out))
    return dump


async def dump_sidebar(page) -> dict:
    """Cammina il pannello laterale (sola lettura, nessuna chat aperta)."""
    await page.wait_for_timeout(5000)  # lascia idratare la lista
    dump = None
    for sel in PANE_CANDIDATES:
        dump = await page.evaluate(JS_DUMP, sel)
        if not dump.get("error") and dump.get("rowCount"):
            break
    if not dump or dump.get("error"):
        await snap(page, "poc3-pane-non-trovato")
        raise SystemExit(f"Pannello chat non trovato con nessun candidato: {dump}")

    # Mascheramento: testi e attributi title/aria-label contengono nomi e numeri.
    def scrub(node):
        node["text"] = mask_pii(node.get("text", ""), keep=80)
        for k in list(node.get("attrs", {})):
            node["attrs"][k] = mask_pii(node["attrs"][k], keep=60)
        return node

    for row in dump["rows"]:
        scrub(row["self"])
        row["children"] = [scrub(c) for c in row["children"]]

    out = artifacts_dir() / f"dom_dump_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
    log_event("dom_dump", pane=dump["sel"], rows=dump["rowCount"], file=str(out))
    print(f"Dump scritto in {out} — {dump['rowCount']} righe viste.")
    print("Ora compila docs/whatsapp/wa-dom-catalog.md guardando questo file.")
    return dump


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat", default=None,
                         help="Numero (E.164 o locale) di una chat controllata in allowlist: "
                              "dump del pannello conversazione (Step 1b) invece della sidebar.")
    args = parser.parse_args()

    async with wa_context(headless=False) as (context, page):
        if args.chat:
            dump = await dump_conversazione(page, args.chat)
            print(f"Dump conversazione scritto — counts={dump['counts']}")
            print("Ora compila la sezione 'Pannello conversazione' di docs/whatsapp/wa-dom-catalog.md.")
        else:
            await dump_sidebar(page)


if __name__ == "__main__":
    asyncio.run(main())
