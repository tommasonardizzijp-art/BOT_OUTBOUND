# backend/scripts/poc_wa/probe_selettori.py
"""Probe diagnostico: quali selettori candidati agganciano davvero il DOM.

Nasce dal fallimento reale del 27/07: `poc1_login.py` ha dichiarato "ne' lista
chat ne' QR" mentre lo screenshot mostrava una sessione perfettamente loggata.
Nessuno dei CHATLIST_CANDIDATES agganciava, e il QR non era un <canvas>. E' il
rischio che il Task 5 esisteva per scoprire, arrivato al primo contatto col DOM
vero invece che al momento comodo.

Cosa fa: apre il profilo, aspetta che la pagina si stabilizzi, e per OGNI
selettore candidato usato dagli script PoC riporta quanti nodi aggancia.
Poi elenca i contenitori plausibili trovati per struttura, cosi' i selettori
veri si LEGGONO invece di indovinarli.

SOLA LETTURA. Non clicca, non apre chat, non scrive niente su WhatsApp.
L'unico output sta in artifacts/.

Uso:  python probe_selettori.py
"""
import asyncio
import json

from _common import artifacts_dir, log_event, snap, wa_context

# I candidati usati oggi dagli script PoC, raccolti in un posto solo.
# Ogni gruppo dice a quale script serve: se una riga e' 0, quello script e' rotto.
CANDIDATI = {
    "poc1_login / lista chat": [
        "#pane-side",
        "[data-testid='chat-list']",
        "div[aria-label*='Elenco chat']",
        "div[aria-label*='Chat list']",
        "[role='grid']",
    ],
    "poc1_login / QR": [
        "canvas[aria-label*='Scan']",
        "[data-testid='qrcode']",
        "canvas",
    ],
    "poc3 / righe chat": [
        "[role='listitem']",
        "[role='row']",
        "#pane-side [role='row']",
        "div[data-testid='cell-frame-container']",
    ],
    "poc2 / ricerca e composer": [
        "[role='textbox']",
        "div[contenteditable='true']",
        "[data-testid='chat-list-search']",
        "footer [contenteditable='true']",
    ],
    "poc2 / GUARDIA PRE-INVIO (JS_TAIL)": [
        "div.message-in",
        "div.message-out",
        "#main",
        "[data-id]",
    ],
    "poc2 / spunte": [
        "[data-icon='status-dblcheck']",
        "[data-icon='status-check']",
        "[data-icon='status-time']",
        "[data-icon]",
    ],
}

# Descrive la struttura reale: da qui escono i selettori veri per il catalogo.
JS_STRUTTURA = """
() => {
  const out = {};

  // 1. I contenitori con piu' figli diretti: la lista chat e' quasi sempre
  //    il nodo con decine di figli omogenei.
  const grossi = [];
  document.querySelectorAll('div, ul, section, main, aside').forEach(el => {
    const n = el.children.length;
    if (n >= 8) {
      const attrs = {};
      for (const a of el.attributes) {
        if (a.name.startsWith('data-') || a.name.startsWith('aria-')
            || a.name === 'role' || a.name === 'id' || a.name === 'class') {
          attrs[a.name] = a.value.slice(0, 120);
        }
      }
      grossi.push({tag: el.tagName.toLowerCase(), figli: n, attrs});
    }
  });
  grossi.sort((a, b) => b.figli - a.figli);
  out.contenitori = grossi.slice(0, 12);

  // 2. Tutti i role presenti e quante volte: role='listitem'/'row'/'grid'
  //    dicono subito come e' costruita la lista.
  const roles = {};
  document.querySelectorAll('[role]').forEach(el => {
    const r = el.getAttribute('role');
    roles[r] = (roles[r] || 0) + 1;
  });
  out.roles = roles;

  // 3. Tutti i data-* usati nella pagina: i data-testid/data-icon sono i
  //    selettori piu' stabili quando esistono.
  const datakeys = {};
  document.querySelectorAll('*').forEach(el => {
    for (const a of el.attributes) {
      if (a.name.startsWith('data-')) {
        datakeys[a.name] = (datakeys[a.name] || 0) + 1;
      }
    }
  });
  out.data_attributes = datakeys;

  // 4. I valori di data-icon: da qui escono le spunte (Q39).
  const icons = {};
  document.querySelectorAll('[data-icon]').forEach(el => {
    const v = el.getAttribute('data-icon');
    icons[v] = (icons[v] || 0) + 1;
  });
  out.data_icon_values = icons;

  // 5. Le classi che compaiono di piu': 'message-in'/'message-out' sono
  //    classi, non data-attribute, e vanno cercate cosi'.
  const classi = {};
  document.querySelectorAll('[class]').forEach(el => {
    el.classList.forEach(c => { classi[c] = (classi[c] || 0) + 1; });
  });
  const top = Object.entries(classi).sort((a, b) => b[1] - a[1]).slice(0, 40);
  out.classi_frequenti = Object.fromEntries(top);

  out.titolo = document.title;
  out.url = location.href;
  return out;
}
"""


async def main() -> None:
    async with wa_context(headless=False) as (context, page):
        # La pagina appena caricata non ha ancora costruito la lista: senza
        # questa attesa il probe misurerebbe lo scheletro, non l'app. E'
        # anche la causa candidata del falso "schermata ignota" del 27/07.
        print("Attendo 20s che WhatsApp Web finisca di costruirsi…")
        await page.wait_for_timeout(20000)

        risultati = {}
        for gruppo, selettori in CANDIDATI.items():
            risultati[gruppo] = {}
            for sel in selettori:
                try:
                    n = await page.locator(sel).count()
                except Exception as exc:
                    n = f"ERRORE: {type(exc).__name__}"
                risultati[gruppo][sel] = n

        print("\n=== SELETTORI CANDIDATI (0 = non aggancia nulla) ===")
        for gruppo, esiti in risultati.items():
            print(f"\n[{gruppo}]")
            for sel, n in esiti.items():
                segno = "OK " if isinstance(n, int) and n > 0 else "-- "
                print(f"  {segno} {n:>5}  {sel}" if isinstance(n, int) else f"  ?? {n}  {sel}")

        struttura = await page.evaluate(JS_STRUTTURA)

        print("\n=== ROLE PRESENTI ===")
        for r, n in sorted(struttura["roles"].items(), key=lambda x: -x[1]):
            print(f"  {n:>5}  role='{r}'")

        print("\n=== data-* PRESENTI ===")
        for k, n in sorted(struttura["data_attributes"].items(), key=lambda x: -x[1])[:25]:
            print(f"  {n:>5}  {k}")

        print("\n=== data-icon (valori) ===")
        for k, n in sorted(struttura["data_icon_values"].items(), key=lambda x: -x[1])[:30]:
            print(f"  {n:>5}  data-icon='{k}'")

        print("\n=== CONTENITORI PIU' POPOLATI (candidati lista chat) ===")
        for c in struttura["contenitori"]:
            print(f"  {c['figli']:>4} figli  <{c['tag']}>  {json.dumps(c['attrs'], ensure_ascii=False)[:200]}")

        print("\n=== CLASSI PIU' FREQUENTI ===")
        for c, n in list(struttura["classi_frequenti"].items())[:25]:
            print(f"  {n:>5}  .{c}")

        # Su disco resta tutto: il catalogo si compila da qui, non dal terminale.
        # Nessun testo di messaggio viene letto, quindi non c'e' PII da mascherare:
        # solo nomi di selettori, conteggi e attributi strutturali.
        out = artifacts_dir() / "probe_selettori.json"
        out.write_text(
            json.dumps({"candidati": risultati, "struttura": struttura}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        await snap(page, "probe-selettori")
        log_event("probe_selettori_done", file=str(out))
        print(f"\nDump completo: {out}")


if __name__ == "__main__":
    asyncio.run(main())
