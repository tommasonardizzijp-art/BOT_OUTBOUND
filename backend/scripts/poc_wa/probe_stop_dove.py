# backend/scripts/poc_wa/probe_stop_dove.py
"""DOVE sta lo STOP nella conversazione, e dove si ferma la guardia.

Il probe della guardia mostrava solo gli ultimi 16 messaggi: non bastava a
capire se uno STOP piu' indietro fosse stato mancato per il LIMITE di risalita
(quanti messaggi legge) o per il CRITERIO di arresto (si ferma al primo
messaggio nostro). Sono due difetti diversi con due fix diversi.

Questo probe dumpa TUTTI i messaggi caricati, in ordine, con:
  - direzione (stessa logica di JS_TAIL)
  - se contengono uno STOP secondo contains_stop, la funzione vera
  - dove JS_TAIL smette di leggere

Fa anche uno screenshot a piena pagina della conversazione.

SOLA LETTURA, nessun invio.

Uso:  python probe_stop_dove.py --numero "+39..."
"""
import argparse
import asyncio
import json

from _common import (apri_chat_da_risultati, artifacts_dir, carica_cronologia, first_locator,
                     human_type, log_event, snap, svuota_ricerca, wa_context)
from wa_lib import AllowList, contains_stop, mask_pii, normalize_e164

JS_TUTTI = """
() => {
  const rows = Array.from(document.querySelectorAll(
    "#main [data-id], #main [data-testid^='conv-msg-']"));
  return rows.map((el, i) => {
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
      i,
      verdetto: (out && !inn) ? 'OUT' : 'IN',
      testo: (el.innerText || '').slice(0, 120),
      // un messaggio multimediale ha testo povero o vuoto: va distinto da
      // "non sono riuscito a leggerlo"
      ha_media: !!el.querySelector("[data-icon='ptt-status'], audio, video, img[src^='blob'], [aria-label*='immagine' i], [aria-label*='vocale' i]"),
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
        await svuota_ricerca(page, box[0])
        await human_type(page, box[0], e164)
        await page.wait_for_timeout(2500)
        aperto, nota = await apri_chat_da_risultati(page)
        if not aperto:
            raise SystemExit(f"Chat non aperta: {nota}")
        await page.wait_for_timeout(4000)

        # Carica la cronologia: senza, nel DOM restano solo i messaggi visibili
        # e uno STOP di 20 minuti prima non c'e' proprio.
        info = await carica_cronologia(page, minimo=80)
        print(f"Cronologia caricata: {info}")

        msgs = await page.evaluate(JS_TUTTI)
        await snap(page, "probe-stop-dove")

        # Dove si fermerebbe JS_TAIL: risalendo dal fondo, al primo OUT.
        stop_index = None
        for m in reversed(msgs):
            if m["verdetto"] == "OUT":
                stop_index = m["i"]
                break

        print(f"\nMessaggi caricati nel DOM: {len(msgs)}")
        print(f"JS_TAIL risale dal fondo e si ferma all'indice {stop_index} (primo OUT dal basso).")
        print(f"Quindi legge solo gli indici {stop_index + 1 if stop_index is not None else 0}..{len(msgs) - 1}\n")

        print(f"{'idx':>4} {'dir':4} {'STOP?':6} {'media':6} testo")
        trovati = []
        for m in msgs:
            s = contains_stop(m["testo"])
            if s and m["verdetto"] == "IN":
                trovati.append(m["i"])
            marca = "<< STOP" if s else ""
            letto = "" if stop_index is None or m["i"] > stop_index else "  (non letto dalla guardia)"
            print(f"{m['i']:>4} {m['verdetto']:4} {str(s):6} {str(m['ha_media']):6} "
                  f"{mask_pii(m['testo'].replace(chr(10), ' | '), keep=60)!r} {marca}{letto}")

        print(f"\nSTOP inbound trovati agli indici: {trovati or 'NESSUNO'}")
        if trovati and stop_index is not None:
            dentro = [i for i in trovati if i > stop_index]
            fuori = [i for i in trovati if i <= stop_index]
            print(f"  visti dalla guardia : {dentro or 'NESSUNO'}")
            print(f"  NON visti (troppo in alto rispetto al nostro ultimo messaggio): {fuori or 'nessuno'}")
            if fuori and not dentro:
                print("\n  DIAGNOSI: lo STOP esiste ed e' leggibile, ma sta PRIMA di un nostro")
                print("  messaggio. Non e' un limite di quanti messaggi si leggono: e' il")
                print("  CRITERIO DI ARRESTO (fermarsi al primo messaggio nostro) a nasconderlo.")

        out = artifacts_dir() / "probe_stop_dove.json"
        for m in msgs:
            m["testo"] = mask_pii(m["testo"], keep=120)
        out.write_text(json.dumps({"messaggi": msgs, "stop_index": stop_index,
                                   "stop_trovati": trovati}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        log_event("probe_stop_dove", messaggi=len(msgs), stop_trovati=len(trovati))
        print(f"\nDump: {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--numero", required=True)
    args = ap.parse_args()
    asyncio.run(main(args.numero))
