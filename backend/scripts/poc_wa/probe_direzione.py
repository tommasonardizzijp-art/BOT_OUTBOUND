# backend/scripts/poc_wa/probe_direzione.py
"""Trova il discriminante INBOUND/OUTBOUND nel DOM di WhatsApp Web.

E' la domanda piu' importante di M0. La guardia pre-invio deve leggere i
messaggi ricevuti DOPO il nostro ultimo messaggio: senza sapere distinguere
"in" da "out" non esiste ne' "il nostro ultimo messaggio" ne' "i messaggi
ricevuti", e la guardia non e' scrivibile affatto.

Il 27/07 il probe conversazione ha dimostrato che `div.message-in` /
`div.message-out` NON esistono piu'. Questo script cerca cosa li ha sostituiti:
per ogni messaggio risale la catena dei genitori e scende nei figli, e raccoglie
TUTTI gli attributi, cercando quelli che si dividono in due gruppi coerenti.

Poi verifica i candidati contro un fatto noto: i messaggi con `tail-out` sono
certamente nostri, quelli con `tail-in` certamente dell'altro. Se un attributo
separa correttamente quei due insiemi, e' il discriminante.

SOLA LETTURA. Apre una chat in allowlist, non digita, non invia.
I testi sono mascherati e troncati a 40 caratteri: qui serve la struttura,
non il contenuto delle conversazioni di nessuno.

Uso:  python probe_direzione.py --indice 1
"""
import argparse
import asyncio
import json

from _common import artifacts_dir, first_locator, human_type, log_event, snap, wa_context
from wa_lib import AllowList, mask_pii, normalize_e164

RICERCA_CANDIDATES = ["[role='textbox']", "[data-testid='chat-list-search']"]

JS_DIREZIONE = """
() => {
  const msgs = Array.from(document.querySelectorAll('#main [data-id]'));
  const descrivi = (el) => {
    const a = {};
    for (const at of el.attributes) a[at.name] = at.value.slice(0, 160);
    return {tag: el.tagName.toLowerCase(), attrs: a};
  };

  return msgs.slice(-14).map(el => {
    // Catena dei genitori fino a #main: la direzione e' spesso su un wrapper,
    // non sulla bolla che porta il data-id.
    const antenati = [];
    let p = el.parentElement;
    let salti = 0;
    while (p && salti < 5 && p.id !== 'main') {
      antenati.push(descrivi(p));
      p = p.parentElement;
      salti++;
    }

    // Figli diretti + i nodi che portano attributi interessanti.
    const figli = Array.from(el.children).map(descrivi);
    const interni = Array.from(el.querySelectorAll('[data-pre-plain-text], [data-icon], [aria-label]'))
      .slice(0, 8).map(descrivi);

    return {
      self: descrivi(el),
      antenati,
      figli,
      interni,
      testo: (el.innerText || '').slice(0, 40),
      icone: Array.from(el.querySelectorAll('[data-icon]')).map(i => i.getAttribute('data-icon')),
    };
  });
}
"""


def _maschera_nodo(n: dict) -> dict:
    """data-pre-plain-text e aria-label contengono nome e orario del mittente."""
    a = {k: (mask_pii(v, keep=80) if isinstance(v, str) else v) for k, v in n.get("attrs", {}).items()}
    return {**n, "attrs": a}


async def main(numero: str) -> None:
    e164 = normalize_e164(numero)
    if not e164:
        raise SystemExit(f"Numero non normalizzabile: {numero!r}")
    AllowList.load().assert_allowed(e164)

    async with wa_context(headless=False) as (context, page):
        await page.wait_for_timeout(20000)
        trovato = await first_locator(page, RICERCA_CANDIDATES, timeout_ms=20000)
        if not trovato:
            raise SystemExit("Casella di ricerca non trovata.")
        ricerca, _ = trovato
        # human_type, non delay fisso: vedi nota in probe_conversazione.py.
        await human_type(page, ricerca, e164)
        await page.wait_for_timeout(2500)

        righe = page.locator("[role='row']")
        n = await righe.count()
        testi = [(await righe.nth(i).inner_text()).strip() for i in range(n)]
        idx = next((i for i, t in enumerate(testi) if t.lower() in ("chat", "chats")), None)
        if idx is None or idx + 1 >= n:
            raise SystemExit("Nessuna conversazione esistente per questo numero.")
        await righe.nth(idx + 1).click()
        await page.wait_for_timeout(3500)

        dati = await page.evaluate(JS_DIREZIONE)
        for m in dati:
            m["self"] = _maschera_nodo(m["self"])
            m["antenati"] = [_maschera_nodo(x) for x in m["antenati"]]
            m["figli"] = [_maschera_nodo(x) for x in m["figli"]]
            m["interni"] = [_maschera_nodo(x) for x in m["interni"]]
            m["testo"] = mask_pii(m["testo"], keep=40)

        # --- ricerca automatica del discriminante ---------------------------
        # Verita' di riferimento: tail-out => nostro, tail-in => loro.
        noti_out = [m for m in dati if "tail-out" in m["icone"]]
        noti_in = [m for m in dati if "tail-in" in m["icone"]]
        print(f"Messaggi campionati: {len(dati)} · certi OUT: {len(noti_out)} · certi IN: {len(noti_in)}")

        def chiavi(m):
            """Tutti gli (attributo, valore) osservabili su un messaggio,
            self + antenati, appiattiti in un insieme confrontabile."""
            s = set()
            for nodo in [m["self"], *m["antenati"]]:
                for k, v in nodo["attrs"].items():
                    if k == "class":
                        for c in v.split():
                            s.add(("class", c))
                    elif k != "data-id":     # unico per messaggio, inutile
                        s.add((k, v))
            return s

        if noti_out and noti_in:
            comuni_out = set.intersection(*[chiavi(m) for m in noti_out])
            comuni_in = set.intersection(*[chiavi(m) for m in noti_in])
            solo_out = comuni_out - comuni_in
            solo_in = comuni_in - comuni_out
            print("\n=== CANDIDATI DISCRIMINANTE ===")
            print("Presenti su TUTTI gli OUT e su NESSUN IN:")
            for k, v in sorted(solo_out):
                print(f"   {k} = {v[:80]}")
            print("Presenti su TUTTI gli IN e su NESSUN OUT:")
            for k, v in sorted(solo_in):
                print(f"   {k} = {v[:80]}")
        else:
            print("\nCampione insufficiente: servono messaggi con tail-in E tail-out.")

        print("\n=== STRUTTURA DEI PRIMI 3 MESSAGGI ===")
        for m in dati[:3]:
            print(f"\n--- icone={m['icone']} testo={m['testo']!r}")
            print(f"  self     : {json.dumps(m['self'], ensure_ascii=False)[:300]}")
            for i, an in enumerate(m["antenati"][:3]):
                print(f"  antenato{i}: {json.dumps(an, ensure_ascii=False)[:300]}")
            for it in m["interni"][:3]:
                print(f"  interno  : {json.dumps(it, ensure_ascii=False)[:300]}")

        out = artifacts_dir() / "probe_direzione.json"
        out.write_text(json.dumps(dati, ensure_ascii=False, indent=2), encoding="utf-8")
        await snap(page, "probe-direzione")
        log_event("probe_direzione_done", campioni=len(dati))
        print(f"\nDump completo: {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--numero")
    g.add_argument("--indice", type=int)
    args = ap.parse_args()
    if args.numero:
        target = args.numero
    else:
        numeri = sorted(AllowList.load()._numbers)
        target = numeri[args.indice - 1]
    asyncio.run(main(target))
