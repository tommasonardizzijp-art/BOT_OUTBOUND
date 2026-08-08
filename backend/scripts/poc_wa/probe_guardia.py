# backend/scripts/poc_wa/probe_guardia.py
"""Esegue la GUARDIA PRE-INVIO vera (JS_TAIL di poc2_send) su una chat reale.

Non simula: importa esattamente lo stesso JS che girera' prima di ogni invio.
Se questo probe non aggancia, la guardia non aggancia.

Risponde a due domande:
  1. JS_TAIL ritorna qualcosa di sensato, o `null` (cecita')?
  2. La direzione e' classificata come deve? Ogni messaggio viene stampato con
     i tre segnali separati, cosi' una discordanza si vede invece di sparire
     dentro il verdetto finale.

Include il TEST NEGATIVO: cerca uno STOP nella coda letta con la stessa
`contains_stop` del sender. Se in chat c'e' uno STOP e la guardia non lo vede,
si vede qui e non su un invio vero.

SOLA LETTURA. Non digita nel composer, non invia.

Uso:  python probe_guardia.py --indice 1
"""
import argparse
import asyncio

from _common import first_locator, human_type, log_event, snap, wa_context
from poc2_send import JS_TAIL, TICK_SEL
from wa_lib import AllowList, contains_stop, mask_pii, normalize_e164

# Stessa logica di JS_TAIL, ma espone i segnali invece del solo verdetto:
# serve a vedere SU COSA la guardia ha deciso, non solo cosa ha deciso.
JS_SEGNALI = """
() => {
  const rows = Array.from(document.querySelectorAll(
    "#main [data-id], #main [data-testid^='conv-msg-']"));
  return rows.slice(-16).map(el => {
    const id = el.getAttribute('data-id') || '';
    const lab = el.querySelector("span[aria-label$=':']");
    const label = lab ? lab.getAttribute('aria-label') : null;
    let out = false, inn = false;
    if (el.querySelector("span[aria-label='Tu:']")) out = true;
    if (lab && label !== 'Tu:') inn = true;
    if (el.querySelector("[data-icon='tail-out']")) out = true;
    if (el.querySelector("[data-icon='tail-in']")) inn = true;
    if (/^3A/.test(id) && id.length <= 24) inn = true;
    else if (/^A5/.test(id) || id.length >= 30) out = true;
    return {
      id_prefix: id.slice(0, 2), id_len: id.length,
      etichetta: label,
      coda: el.querySelector("[data-icon='tail-out']") ? 'out'
          : (el.querySelector("[data-icon='tail-in']") ? 'in' : null),
      segnale_out: out, segnale_in: inn,
      verdetto: (out && !inn) ? 'OUT' : 'IN',
      testo: (el.innerText || '').slice(0, 50),
    };
  });
}
"""


async def main(numero: str) -> None:
    e164 = normalize_e164(numero)
    AllowList.load().assert_allowed(e164)

    async with wa_context(headless=False) as (context, page):
        await page.wait_for_timeout(20000)
        box = await first_locator(page, ["[role='textbox']"], timeout_ms=20000)
        if not box:
            raise SystemExit("Casella di ricerca non trovata.")
        await human_type(page, box[0], e164)
        await page.wait_for_timeout(2500)

        righe = page.locator("[role='row']")
        n = await righe.count()
        testi = [(await righe.nth(i).inner_text()).strip() for i in range(n)]
        idx = next((i for i, t in enumerate(testi) if t.lower() in ("chat", "chats")), None)
        if idx is None or idx + 1 >= n:
            raise SystemExit("Nessuna conversazione esistente per questo numero.")
        await righe.nth(idx + 1).click()
        await page.wait_for_timeout(3500)

        # --- segnali per messaggio ----------------------------------------
        segnali = await page.evaluate(JS_SEGNALI)
        print("\n=== CLASSIFICAZIONE PER MESSAGGIO (ultimi 16) ===")
        print(f"{'verdetto':9} {'id':>8} {'coda':5} {'etichetta':18} testo")
        discordanti = 0
        for s in segnali:
            if s["segnale_out"] and s["segnale_in"]:
                discordanti += 1
            eti = (s["etichetta"] or "-")[:18]
            print(f"{s['verdetto']:9} {s['id_prefix']}/{s['id_len']:<5} "
                  f"{(s['coda'] or '-'):5} {eti:18} {mask_pii(s['testo'], keep=40)!r}")
        print(f"\nMessaggi con segnali DISCORDANTI (out e in insieme): {discordanti}")
        if discordanti:
            print("  -> per la regola asimmetrica valgono INBOUND: si legge, non ci si ferma.")

        # --- la guardia vera ----------------------------------------------
        tail = await page.evaluate(JS_TAIL)
        print("\n=== JS_TAIL (la guardia vera) ===")
        if tail is None:
            print("  null -> CECITA'. Il sender abortisce l'invio (sentinella). "
                  "La guardia NON funziona: da correggere prima di qualsiasi invio.")
        else:
            print(f"  {len(tail)} messaggi inbound letti dopo il nostro ultimo messaggio:")
            for t in tail:
                print(f"    - {mask_pii(t, keep=60)!r}")
            stop = any(contains_stop(t) for t in tail)
            print(f"\n  contains_stop sulla coda -> {stop}")
            print("  " + ("STOP RILEVATO: il sender NON invierebbe." if stop
                          else "nessuno STOP: il sender procederebbe."))

        # --- spunte (Q39) --------------------------------------------------
        trovata = await first_locator(page, TICK_SEL, timeout_ms=5000)
        print("\n=== SPUNTE (Q39) ===")
        print(f"  {'trovata con ' + trovata[1] if trovata else 'NESSUN selettore aggancia: Q39 resta aperta'}")

        await snap(page, "probe-guardia")
        log_event("probe_guardia_done",
                  tail_null=(tail is None), inbound_letti=(len(tail) if tail else 0),
                  discordanti=discordanti)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--numero")
    g.add_argument("--indice", type=int)
    args = ap.parse_args()
    # --indice segue l'ordine del file poc.env, non l'alfabeto.
    target = args.numero or AllowList.load().per_indice(args.indice)
    asyncio.run(main(target))
