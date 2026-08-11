# backend/scripts/registra_scroll_umano.py
"""Registra i gesti di scorrimento VERI di Tommaso, per smettere di modellarli.

I parametri di `pagina.piano_scroll` e `pagina.piano_lancio` oggi sono un
modello plausibile — decelerazione esponenziale per il flick, campana per lo
scorrimento controllato — ma nessuno li ha misurati. E' la stessa scommessa
che con le coordinate del DOM e' costata un modulo che non salvava niente.

Questo apre una finestra con una lista finta e ascolta gli eventi `wheel`
mentre la scorri a mano. Da li' escono i numeri veri: quanti pixel per evento,
ogni quanti millisecondi, e come decelera davvero un lancio sul TUO hardware.
Fa anche una cosa che nessun modello puo' indovinare: dice se il dispositivo
si comporta da rotella (scatti discreti, tutti uguali, nessuna inerzia) o da
trackpad (decine di eventi piccoli con momentum). Sono due firme diverse, e
imitare quella sbagliata e' peggio che non imitare niente.

Uso (dal folder backend). Non tocca Instagram, non serve nessun account:
    ./venv/Scripts/python.exe scripts/registra_scroll_umano.py

Poi, nella finestra che si apre: scorri la lista come faresti normalmente,
alterna qualche scorrimento breve a qualche lancio deciso, per una ventina di
secondi. Chiudi la finestra quando hai finito.
"""
import asyncio
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from patchright.async_api import async_playwright   # il bot usa il fork stealth

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "scroll_umano.json")

PAGINA = """
<html><head><meta charset="utf-8"><style>
  body { font-family: system-ui, sans-serif; margin: 0; background: #fafafa; }
  #testata { padding: 14px 18px; background: #111; color: #eee; position: sticky; top: 0; }
  #lista { height: 80vh; overflow-y: auto; border-top: 1px solid #ddd; }
  .riga { height: 72px; padding: 12px 18px; border-bottom: 1px solid #eee;
          display: flex; align-items: center; gap: 12px; background: #fff; }
  .pallino { width: 44px; height: 44px; border-radius: 50%; background: #ddd; }
  #conta { font-variant-numeric: tabular-nums; }
</style></head><body>
  <div id="testata">
    Scorri questa lista come faresti normalmente — qualche scorrimento breve,
    qualche lancio deciso. Eventi registrati: <b id="conta">0</b>
  </div>
  <div id="lista"></div>
</body></html>
"""

# Il listener si installa con evaluate, non con uno <script> dentro set_content:
# sotto patchright quello inline non viene eseguito e window.__eventi resta
# undefined — verificato, e senza questo controllo il registratore avrebbe
# chiuso la finestra dicendo "nessun evento" con la persona convinta di aver
# appena scorso per venti secondi.
JS_PREPARA = """() => {
  const lista = document.getElementById('lista');
  for (let i = 0; i < 400; i++) {
    const r = document.createElement('div');
    r.className = 'riga';
    r.innerHTML = '<div class="pallino"></div><div><b>Contatto ' + i +
                  '</b><br><span style="color:#777">ultimo messaggio...</span></div>';
    lista.appendChild(r);
  }
  window.__eventi = [];
  lista.addEventListener('wheel', (e) => {
    window.__eventi.push({
      t: performance.now(),
      deltaY: e.deltaY,
      modo: e.deltaMode,          // 0 = pixel, 1 = righe, 2 = pagine
      scrollTop: Math.round(lista.scrollTop),
    });
    document.getElementById('conta').textContent = window.__eventi.length;
  }, {passive: true});
  return typeof window.__eventi;
}"""


def raggruppa_in_gesti(eventi, stacco_ms=180):
    """Un gesto finisce quando passano piu' di `stacco_ms` senza eventi."""
    gesti, corrente = [], []
    for e in eventi:
        if corrente and e["t"] - corrente[-1]["t"] > stacco_ms:
            gesti.append(corrente)
            corrente = []
        corrente.append(e)
    if corrente:
        gesti.append(corrente)
    return gesti


def analizza(eventi):
    if not eventi:
        print("nessun evento registrato")
        return {}

    delta = [abs(e["deltaY"]) for e in eventi]
    intervalli = [b["t"] - a["t"] for a, b in zip(eventi, eventi[1:])
                  if 0 < b["t"] - a["t"] < 400]
    modi = {e["modo"] for e in eventi}
    distinti = sorted({round(d) for d in delta})

    print(f"\n=== {len(eventi)} eventi wheel ===")
    print(f"  deltaY: min {min(delta):.0f}  mediana {statistics.median(delta):.0f}  max {max(delta):.0f}")
    print(f"  valori distinti di deltaY: {len(distinti)}  -> {distinti[:12]}{' ...' if len(distinti) > 12 else ''}")
    print(f"  deltaMode: {modi}  (0=pixel, 1=righe, 2=pagine)")
    if intervalli:
        print(f"  intervallo fra eventi: mediana {statistics.median(intervalli):.0f}ms  "
              f"min {min(intervalli):.0f}  max {max(intervalli):.0f}")

    # Rotella o trackpad? Una rotella ha pochissimi valori distinti (multipli di
    # una tacca) e non produce code di decelerazione.
    if len(distinti) <= 3:
        verdetto = "ROTELLA: scatti discreti, nessuna inerzia da imitare"
    elif len(distinti) > 15 and min(delta) < 15:
        verdetto = "TRACKPAD: eventi fini e continui, il momentum esiste"
    else:
        verdetto = "misto/incerto: guardare i gesti qui sotto"
    print(f"  --> {verdetto}")

    gesti = raggruppa_in_gesti(eventi)
    print(f"\n=== {len(gesti)} gesti riconosciuti ===")
    riepilogo = []
    for i, g in enumerate(gesti[:12]):
        d = [abs(e["deltaY"]) for e in g]
        durata = g[-1]["t"] - g[0]["t"]
        percorso = sum(d)
        # Un flick decelera: la seconda meta' del gesto e' piu' lenta della prima.
        meta = max(1, len(d) // 2)
        decelera = (sum(d[meta:]) / max(len(d) - meta, 1)) < (sum(d[:meta]) / meta) * 0.75
        print(f"  gesto {i:>2}: {len(d):>3} eventi  {percorso:>6.0f}px  {durata:>6.0f}ms  "
              f"picco {max(d):>5.0f}px  {'DECELERA (lancio)' if decelera else 'costante (scorrimento)'}")
        riepilogo.append({"eventi": len(d), "px": percorso, "ms": durata,
                          "picco": max(d), "decelera": decelera})

    return {"eventi": eventi, "gesti": riepilogo, "verdetto": verdetto,
            "delta_distinti": distinti,
            "intervallo_mediano_ms": statistics.median(intervalli) if intervalli else None}


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, args=["--start-maximized"])
        page = await (await browser.new_context(no_viewport=True)).new_page()
        await page.set_content(PAGINA)
        pronto = await page.evaluate(JS_PREPARA)
        if pronto != "object":
            print("[X] il registratore non si e' installato: niente da misurare")
            await browser.close()
            return
        print("Finestra aperta. Scorri la lista a mano per una ventina di secondi,")
        print("alternando scorrimenti brevi e lanci decisi, poi CHIUDI la finestra.")

        # Si rilegge tutto a ogni giro invece del solo conteggio: se la finestra
        # viene chiusa nel mezzo, l'ultima copia buona e' gia' in mano nostra.
        eventi = []
        try:
            while not page.is_closed():
                await page.wait_for_timeout(1000)
                try:
                    letti = await page.evaluate("() => window.__eventi")
                except Exception:
                    break
                if letti:
                    eventi = letti
                print(f"  ...{len(eventi)} eventi", end="\r", flush=True)
        except Exception:
            pass

        try:
            await browser.close()
        except Exception:
            pass

    dati = analizza(eventi)
    if dati:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(dati, f, ensure_ascii=False, indent=2)
        print(f"\n[OK] misure salvate in {OUT}")


asyncio.run(main())
