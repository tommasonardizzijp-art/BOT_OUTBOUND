# backend/scripts/poc_wa/probe_conversazione.py
"""Step 1b — discovery del PANNELLO CONVERSAZIONE su UNA chat controllata.

Perche' esiste. `probe_selettori.py` ha catalogato la sidebar, ma la guardia
pre-invio del Task 8 — la garanzia su cui poggia tutto il disegno opt-out —
dipende da tre famiglie di selettori che vivono SOLO a chat aperta: JS_TAIL
(div.message-in / div.message-out), TICK_SEL (spunte), HISTORY_SEL (cronologia).
Il fail-mode che questo probe previene e' il peggiore di M0: se JS_TAIL non
aggancia nulla ritorna lista vuota, la guardia non trova mai uno STOP e
**passa sempre sembrando funzionare**.

Tre guardie, in questo ordine:
  1. il numero DEVE essere in POC_WA_ALLOWED_NUMBERS (fail-closed);
  2. la chat si raggiunge tramite la RICERCA, mai con un deep-link wa.me:
     un deep-link su un numero mai contattato aprirebbe una chat NUOVA, e il
     bot non deve mai creare chat (V2). La ricerca trova solo cio' che esiste;
  3. se la conversazione aperta non contiene messaggi, il probe si ferma e lo
     dichiara: una chat vuota non e' una chat esistente.

NON INVIA NULLA. Non digita nel composer, non preme Enter. Scrive solo su
artifacts/. Aprire la chat la marca come letta: e' accettato perche' la chat
e' di Tommaso (allowlist), ed e' il minimo indispensabile per non inviare alla
cieca. Il watcher di M1 non aprira' mai le chat dei clienti.

Uso:  python probe_conversazione.py --numero "+39..."
      python probe_conversazione.py --indice 1     (n-esimo dell'allowlist)
"""
import argparse
import asyncio
import json

from _common import artifacts_dir, first_locator, human_type, log_event, snap, wa_context
from wa_lib import AllowList, mask_pii, normalize_e164

RICERCA_CANDIDATES = [
    "[role='textbox']",
    "div[contenteditable='true'][role='textbox']",
    "[data-testid='chat-list-search']",
]

# Descrive il pannello conversazione. Registra STRUTTURA e testo TRONCATO e
# MASCHERATO: serve capire se un selettore aggancia il contenuto giusto, non
# archiviare le conversazioni di nessuno.
JS_CONV = """
() => {
  const out = {};

  const main = document.querySelector('#main')
            || document.querySelector("[role='application']")
            || document.querySelector('main');
  out.main_trovato = !!main;
  out.main_selettore = main ? (main.id ? '#' + main.id : main.tagName.toLowerCase()) : null;

  const scope = main || document.body;

  // 1. Quanti nodi agganciano i candidati di JS_TAIL, ORA che la chat e' aperta.
  const prove = {
    'div.message-in': 'div.message-in',
    'div.message-out': 'div.message-out',
    '.message-in (qualsiasi tag)': '.message-in',
    '.message-out (qualsiasi tag)': '.message-out',
    '[data-id]': '[data-id]',
    "[role='row'] dentro main": "[role='row']",
    "[data-testid='msg-container']": "[data-testid='msg-container']",
    '[data-pre-plain-text]': '[data-pre-plain-text]',
  };
  out.conteggi = {};
  for (const [nome, sel] of Object.entries(prove)) {
    try { out.conteggi[nome] = scope.querySelectorAll(sel).length; }
    catch (e) { out.conteggi[nome] = 'ERRORE'; }
  }

  // 2. Tutti i data-* e le classi presenti nel solo pannello conversazione:
  //    da qui esce il selettore vero della bolla, se message-in non regge.
  const datakeys = {}, classi = {};
  scope.querySelectorAll('*').forEach(el => {
    for (const a of el.attributes) {
      if (a.name.startsWith('data-')) datakeys[a.name] = (datakeys[a.name] || 0) + 1;
    }
    if (el.classList) el.classList.forEach(c => { classi[c] = (classi[c] || 0) + 1; });
  });
  out.data_attributes = datakeys;
  out.classi_frequenti = Object.fromEntries(
    Object.entries(classi).sort((a, b) => b[1] - a[1]).slice(0, 30));

  // 3. data-icon presenti a chat aperta: qui devono comparire le spunte,
  //    se esistono (Q39). A chat chiusa non c'erano.
  const icons = {};
  scope.querySelectorAll('[data-icon]').forEach(el => {
    const v = el.getAttribute('data-icon');
    icons[v] = (icons[v] || 0) + 1;
  });
  out.data_icon_values = icons;

  // 4. Le ultime bolle: struttura + testo troncato. Serve per capire COME si
  //    distingue un inbound da un outbound, che e' l'informazione su cui la
  //    guardia si regge. Se message-in/out non esistono, si ripiega su
  //    [data-id], che WhatsApp usa nella forma "false_<chat>@c.us_<id>" per
  //    gli inbound e "true_..." per gli outbound: quel prefisso e' la
  //    distinzione piu' stabile che esista.
  const bolle = scope.querySelectorAll('[data-id], div.message-in, div.message-out');
  const campioni = [];
  const arr = Array.from(bolle);
  for (const el of arr.slice(-12)) {
    const attrs = {};
    for (const a of el.attributes) {
      if (a.name.startsWith('data-') || a.name === 'class' || a.name === 'role') {
        attrs[a.name] = a.value.slice(0, 200);
      }
    }
    campioni.push({
      tag: el.tagName.toLowerCase(),
      attrs,
      testo: (el.innerText || '').slice(0, 80),
      icone: Array.from(el.querySelectorAll('[data-icon]')).map(i => i.getAttribute('data-icon')),
      ha_svg: el.querySelectorAll('svg').length,
    });
  }
  out.campioni_bolle = campioni;
  out.bolle_totali = arr.length;

  // 5. Il composer: serve al Task 8 per digitare. Sola rilevazione.
  const comp = [];
  scope.querySelectorAll("[contenteditable='true']").forEach(el => {
    const attrs = {};
    for (const a of el.attributes) {
      if (a.name.startsWith('data-') || a.name.startsWith('aria-') || a.name === 'role') {
        attrs[a.name] = a.value.slice(0, 120);
      }
    }
    comp.push({tag: el.tagName.toLowerCase(), attrs});
  });
  out.composer = comp;

  return out;
}
"""


async def main(numero: str) -> None:
    e164 = normalize_e164(numero)
    if not e164:
        raise SystemExit(f"Numero non normalizzabile: {numero!r}")
    allow = AllowList.load()
    allow.assert_allowed(e164)          # fail-closed: solleva NotAllowed e si ferma
    masked = mask_pii(e164)

    async with wa_context(headless=False) as (context, page):
        print("Attendo 20s che WhatsApp Web finisca di costruirsi…")
        await page.wait_for_timeout(20000)

        # --- ricerca, MAI deep-link (V2: il bot non crea chat) --------------
        trovato = await first_locator(page, RICERCA_CANDIDATES, timeout_ms=20000)
        if not trovato:
            await snap(page, "probe-conv-ricerca-assente")
            raise SystemExit("Casella di ricerca non trovata: cataloga lo screenshot.")
        ricerca, sel_ricerca = trovato
        log_event("ricerca_trovata", selector=sel_ricerca)

        # human_type e NON keyboard.type(delay=90): un delay fisso significa
        # varianza ZERO tra un tasto e l'altro per dodici cifre di fila, che e'
        # la firma robotica piu' facile da misurare che esista. human_type usa
        # una lognormale con refusi e pause. Vale anche per un probe: se la
        # sessione va bruciata, va bruciata per una ragione vera.
        await human_type(page, ricerca, e164)
        await page.wait_for_timeout(2500)
        await snap(page, "probe-conv-risultati-ricerca")

        # Struttura dei risultati: utile al catalogo anche se poi non si apre.
        risultati_struct = await page.evaluate("""
        () => {
          const rows = Array.from(document.querySelectorAll("[role='row'], [role='listitem'], [role='button']"));
          return rows.slice(0, 12).map(el => {
            const attrs = {};
            for (const a of el.attributes) {
              if (a.name.startsWith('data-') || a.name.startsWith('aria-') || a.name === 'role') {
                attrs[a.name] = a.value.slice(0, 120);
              }
            }
            return {tag: el.tagName.toLowerCase(), attrs, testo: (el.innerText || '').slice(0, 100)};
          });
        }
        """)
        risultati_struct = [
            {**r, "testo": mask_pii(r.get("testo", ""), keep=100)} for r in risultati_struct
        ]

        righe = page.locator("[role='row']")
        n_righe = await righe.count()
        if n_righe == 0:
            await snap(page, "probe-conv-nessun-risultato")
            raise SystemExit(
                f"La ricerca su {masked} non ha prodotto righe: la chat non esiste. "
                f"Il probe NON crea chat nuove (V2). Scrivi prima a mano dal telefono, "
                f"oppure passa un altro numero dell'allowlist."
            )

        # SCOPERTA 27/07: le INTESTAZIONI di sezione ("Chat", "Gruppi in comune",
        # "Contatti", "Messaggi") sono [role='row'] esattamente come le chat vere.
        # Cliccare righe[0] significa cliccare l'intestazione "Chat" e non aprire
        # nulla — e' successo davvero al primo tentativo. Peggio: le righe subito
        # sotto "Gruppi in comune" sono GRUPPI, fuori perimetro (non-goal): una
        # scelta per posizione poteva aprirne uno.
        # Quindi si naviga per SEZIONE: si trova l'intestazione "Chat" e si prende
        # la riga immediatamente successiva, verificando che non sia a sua volta
        # un'intestazione.
        INTESTAZIONI = {"chat", "gruppi in comune", "contatti", "messaggi",
                        "chats", "groups in common", "contacts", "messages"}
        testi = []
        for i in range(n_righe):
            try:
                testi.append((await righe.nth(i).inner_text()).strip())
            except Exception:
                testi.append("")

        idx_chat = next((i for i, t in enumerate(testi) if t.lower() in ("chat", "chats")), None)
        if idx_chat is None:
            await snap(page, "probe-conv-nessuna-sezione-chat")
            raise SystemExit(
                f"Nessuna sezione 'Chat' nei risultati per {masked}: esistono solo "
                f"gruppi o contatti senza conversazione. Il probe non apre gruppi "
                f"(fuori perimetro) e non crea chat. Prova un altro numero."
            )
        target_idx = idx_chat + 1
        if target_idx >= n_righe or testi[target_idx].lower() in INTESTAZIONI:
            await snap(page, "probe-conv-sezione-chat-vuota")
            raise SystemExit(
                f"La sezione 'Chat' per {masked} e' vuota: nessuna conversazione "
                f"esistente con questo numero. Scrivigli prima a mano dal telefono."
            )

        prima_riga = mask_pii(testi[target_idx].replace("\n", " | "), keep=60)
        print(f"Risultati ricerca: {n_righe} righe. Sezione 'Chat' a indice {idx_chat}; "
              f"apro la riga {target_idx}: {prima_riga}")
        log_event("apro_chat", indice=target_idx, anteprima=prima_riga)
        await righe.nth(target_idx).click()
        await page.wait_for_timeout(3500)
        await snap(page, "probe-conv-chat-aperta")

        conv = await page.evaluate(JS_CONV)
        conv["campioni_bolle"] = [
            {**b, "testo": mask_pii(b.get("testo", ""), keep=80)} for b in conv.get("campioni_bolle", [])
        ]

        # --- referto -------------------------------------------------------
        print(f"\n#main trovato: {conv['main_trovato']}  ({conv['main_selettore']})")
        print(f"Bolle agganciate in totale: {conv['bolle_totali']}")

        print("\n=== CANDIDATI JS_TAIL (a chat APERTA) ===")
        for nome, n in conv["conteggi"].items():
            segno = "OK " if isinstance(n, int) and n > 0 else "-- "
            print(f"  {segno} {n:>5}  {nome}")

        print("\n=== data-icon a chat aperta (qui devono comparire le spunte) ===")
        if conv["data_icon_values"]:
            for k, n in sorted(conv["data_icon_values"].items(), key=lambda x: -x[1]):
                print(f"  {n:>5}  data-icon='{k}'")
        else:
            print("  NESSUNO: Q39 (lettura spunta) non risolvibile via data-icon.")

        print("\n=== data-* nel pannello conversazione ===")
        for k, n in sorted(conv["data_attributes"].items(), key=lambda x: -x[1])[:20]:
            print(f"  {n:>5}  {k}")

        print("\n=== ULTIME BOLLE (testo mascherato e troncato) ===")
        for b in conv["campioni_bolle"]:
            cls = b["attrs"].get("class", "")[:60]
            did = b["attrs"].get("data-id", "")[:60]
            print(f"  <{b['tag']}> icone={b['icone']} svg={b['ha_svg']}")
            print(f"      data-id: {did}")
            print(f"      class  : {cls}")
            print(f"      testo  : {b['testo']}")

        print("\n=== COMPOSER ===")
        for c in conv["composer"]:
            print(f"  <{c['tag']}> {json.dumps(c['attrs'], ensure_ascii=False)[:160]}")

        out = artifacts_dir() / "probe_conversazione.json"
        out.write_text(
            json.dumps({"numero_masked": masked, "risultati_ricerca": risultati_struct,
                        "conversazione": conv}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log_event("probe_conversazione_done", numero_masked=masked, bolle=conv["bolle_totali"])
        print(f"\nDump completo: {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--numero", help="numero E.164, deve essere in allowlist")
    g.add_argument("--indice", type=int, help="n-esimo numero dell'allowlist (1-based)")
    args = ap.parse_args()

    # --indice segue l'ORDINE DEL FILE poc.env (vedi AllowList.per_indice):
    # "il primo della lista" deve voler dire la stessa cosa per Tommaso e per
    # il codice.
    target = args.numero or AllowList.load().per_indice(args.indice)

    asyncio.run(main(target))
