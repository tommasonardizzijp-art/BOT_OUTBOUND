# backend/scripts/poc_wa/probe_riga2.py
"""Chiude Q41 (badge non letti) e Q42 (direzione ultimo messaggio in sidebar).

`probe_riga.py` ha catalogato la struttura della riga, ma su un campione di
chat tutte LETTE e senza spunte catturate: cercava solo nodi con attributi, e
le doppie spunte visibili nello screenshot non hanno `data-icon`.

Questo probe:
  - cerca le righe NON LETTE (ce ne sono: il filtro "Da leggere" ne dichiara 64)
    invece di prendere le prime della lista;
  - dentro `cell-frame-secondary` descrive OGNI nodo, anche senza attributi,
    inclusi <svg> e il loro <title>: e' li' che vivono le spunte.

SOLA LETTURA, NON APRE NESSUNA CHAT.

Uso:  python probe_riga2.py
"""
import asyncio
import json

from _common import artifacts_dir, log_event, snap, wa_context
from wa_lib import mask_pii

JS = """
() => {
  const pane = document.querySelector('#pane-side');
  if (!pane) return {errore: '#pane-side non trovato'};
  const righe = Array.from(pane.querySelectorAll("[role='row']"));

  // Tutto l'albero di un nodo, attributi o no: le spunte sono <svg> nudi.
  const albero = (el, prof) => {
    if (!el || prof > 4) return [];
    const fuori = [];
    for (const c of el.children) {
      const a = {};
      for (const at of c.attributes) a[at.name] = at.value.slice(0, 100);
      const svgTitle = c.tagName.toLowerCase() === 'svg'
        ? (c.querySelector('title') ? c.querySelector('title').textContent : null) : null;
      fuori.push({
        prof, tag: c.tagName.toLowerCase(), attrs: a,
        svg_title: svgTitle,
        testo: (c.innerText || '').slice(0, 40),
      });
      fuori.push(...albero(c, prof + 1));
    }
    return fuori;
  };

  const descriviRiga = (r) => ({
    testo: (r.innerText || '').slice(0, 100),
    // aria-label della riga: spesso contiene "N messaggi non letti"
    aria_label_riga: r.getAttribute('aria-label'),
    // il contenitore "secondary" ospita anteprima, orario, badge e spunte
    secondary: albero(r.querySelector("[data-testid='cell-frame-secondary']"), 0),
  });

  // Una riga e' "non letta" se da qualche parte dichiara messaggi non letti.
  const nonLette = righe.filter(r => {
    const html = r.innerHTML;
    return /non lett|unread/i.test(html);
  });

  return {
    righe_totali: righe.length,
    non_lette_trovate: nonLette.length,
    campioni_non_lette: nonLette.slice(0, 3).map(descriviRiga),
    campioni_lette: righe.filter(r => !/non lett|unread/i.test(r.innerHTML))
                         .slice(0, 3).map(descriviRiga),
  };
}
"""


async def main() -> None:
    async with wa_context(headless=False) as (context, page):
        print("Attendo 20s…")
        await page.wait_for_timeout(20000)

        d = await page.evaluate(JS)
        if isinstance(d, dict) and d.get("errore"):
            raise SystemExit(d["errore"])

        def scrub(campioni):
            for c in campioni:
                c["testo"] = mask_pii(c["testo"], keep=90)
                if c.get("aria_label_riga"):
                    c["aria_label_riga"] = mask_pii(c["aria_label_riga"], keep=90)
                for n in c["secondary"]:
                    n["attrs"] = {k: mask_pii(v, keep=90) for k, v in n["attrs"].items()}
                    n["testo"] = mask_pii(n["testo"], keep=40)
                    if n.get("svg_title"):
                        n["svg_title"] = mask_pii(n["svg_title"], keep=60)

        scrub(d["campioni_non_lette"])
        scrub(d["campioni_lette"])

        out = artifacts_dir() / "probe_riga2.json"
        out.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        await snap(page, "probe-riga2")
        log_event("probe_riga2_done", non_lette=d["non_lette_trovate"])

        print(f"\nRighe: {d['righe_totali']} · con marcatore 'non letto': {d['non_lette_trovate']}")
        for etichetta, campioni in (("NON LETTE", d["campioni_non_lette"]),
                                    ("LETTE", d["campioni_lette"])):
            for i, c in enumerate(campioni):
                print(f"\n=== {etichetta} {i} ===")
                print(f"  testo: {c['testo'].replace(chr(10), ' | ')!r}")
                print(f"  aria-label riga: {c['aria_label_riga']!r}")
                for n in c["secondary"]:
                    svg = f" svg_title={n['svg_title']!r}" if n.get("svg_title") else ""
                    attrs = json.dumps(n["attrs"], ensure_ascii=False)[:120]
                    print(f"    [p{n['prof']}] <{n['tag']}> {attrs}{svg} :: {n['testo']!r}")
        print(f"\nDump: {out}")


if __name__ == "__main__":
    asyncio.run(main())
