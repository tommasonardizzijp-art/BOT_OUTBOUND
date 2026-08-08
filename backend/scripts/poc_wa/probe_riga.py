# backend/scripts/poc_wa/probe_riga.py
"""Struttura INTERNA di una riga della lista chat — chiude Q19, Q41, Q42.

`poc3_scan.py` deve leggere, per ogni riga: titolo (nome o numero), badge dei
non letti, anteprima dell'ultimo messaggio e la sua direzione. I suoi selettori
sono ancora quelli ipotizzati a tavolino (`[role='listitem']`, che non esiste,
e `data-icon='status-*'`, che non esiste). Questo probe li sostituisce con
quelli veri invece di indovinarli una seconda volta.

Domande a cui risponde:
  Q19 — il titolo mostra il nome (contatto in rubrica) o il numero?
  Q41 — il badge dei non letti e' leggibile (testo/aria-label) o solo CSS?
  Q42 — si capisce dalla riga se l'ultimo messaggio e' nostro o loro?

SOLA LETTURA e, soprattutto, **NON APRE NESSUNA CHAT**: e' l'unico probe che
rispetta il vincolo del watcher (aprire = marcare letto = interferire col
lavoro del cliente). Legge solo cio' che la sidebar mostra gia'.

Uso:  python probe_riga.py
"""
import asyncio
import json

from _common import artifacts_dir, log_event, snap, wa_context
from wa_lib import mask_pii

JS_RIGHE = """
() => {
  const pane = document.querySelector('#pane-side');
  if (!pane) return {errore: '#pane-side non trovato'};

  const righe = Array.from(pane.querySelectorAll("[role='row']"));

  const descrivi = (el, prof) => {
    const a = {};
    for (const at of el.attributes) {
      if (at.name.startsWith('data-') || at.name.startsWith('aria-')
          || at.name === 'role' || at.name === 'title' || at.name === 'dir') {
        a[at.name] = at.value.slice(0, 120);
      }
    }
    return {prof, tag: el.tagName.toLowerCase(), attrs: a,
            testo: (el.innerText || '').slice(0, 60)};
  };

  return {
    totale_righe: righe.length,
    rowcount_dichiarato: (document.querySelector("[role='grid']") || {}).getAttribute
        ? document.querySelector("[role='grid']").getAttribute('aria-rowcount') : null,
    campioni: righe.slice(0, 8).map(r => {
      // Ogni nodo interno che porti un attributo utile: da qui escono
      // TITLE_SEL, UNREAD_SEL, PREVIEW_SEL, e il segnale di direzione.
      const nodi = [];
      const walk = (el, prof) => {
        if (prof > 6) return;
        for (const c of el.children) {
          const ha = Array.from(c.attributes).some(at =>
            at.name === 'title' || at.name === 'dir'
            || at.name.startsWith('aria-') || at.name.startsWith('data-'));
          if (ha) nodi.push(descrivi(c, prof));
          walk(c, prof + 1);
        }
      };
      walk(r, 0);
      return {
        testo_riga: (r.innerText || '').slice(0, 120),
        icone: Array.from(r.querySelectorAll('[data-icon]')).map(i => i.getAttribute('data-icon')),
        span_con_title: Array.from(r.querySelectorAll('span[title]'))
          .map(s => ({title: s.getAttribute('title').slice(0, 60), testo: (s.innerText||'').slice(0,60)})),
        nodi: nodi.slice(0, 14),
      };
    }),
  };
}
"""


async def main() -> None:
    async with wa_context(headless=False) as (context, page):
        print("Attendo 20s che WhatsApp Web finisca di costruirsi…")
        await page.wait_for_timeout(20000)

        d = await page.evaluate(JS_RIGHE)
        if isinstance(d, dict) and d.get("errore"):
            await snap(page, "probe-riga-fallito")
            raise SystemExit(d["errore"])

        # Si maschera e si SCRIVE PRIMA di stampare: il 27/07 un carattere non
        # codificabile dalla console ha ucciso lo script dopo la raccolta e
        # prima del salvataggio, buttando via il lavoro per un problema di sola
        # visualizzazione. Il dump non deve dipendere dalla riuscita dei print.
        for c in d["campioni"]:
            c["testo_riga"] = mask_pii(c["testo_riga"], keep=120)
            c["span_con_title"] = [{k: mask_pii(v, keep=60) for k, v in s.items()}
                                   for s in c["span_con_title"]]
            for n in c["nodi"]:
                n["attrs"] = {k: mask_pii(v, keep=120) for k, v in n["attrs"].items()}
                n["testo"] = mask_pii(n["testo"], keep=60)

        out = artifacts_dir() / "probe_riga.json"
        out.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        await snap(page, "probe-riga")
        log_event("probe_riga_done", righe=d["totale_righe"])

        print(f"\nRighe nel DOM: {d['totale_righe']} · dichiarate: {d['rowcount_dichiarato']}")

        for i, c in enumerate(d["campioni"]):
            print(f"\n=== RIGA {i} ===")
            print(f"  testo : {c['testo_riga'].replace(chr(10), ' | ')!r}")
            print(f"  icone : {c['icone']}")
            for s in c["span_con_title"]:
                print(f"  span[title]: title={s['title']!r} testo={s['testo']!r}")
            for n in c["nodi"]:
                print(f"    [p{n['prof']}] <{n['tag']}> {json.dumps(n['attrs'], ensure_ascii=False)[:170]}")

        print(f"\nDump completo: {out}")


if __name__ == "__main__":
    asyncio.run(main())
