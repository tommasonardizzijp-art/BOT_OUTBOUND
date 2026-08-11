"""PROBE diagnostico: perche' la verifica post-click legge 'aperto None'?

Il collaudo dell'11/08 ha fallito 4 aperture su 4 con "atteso <nome>, aperto
None", cioe' l'header non e' stato letto AFFATTO. Il PoC-4, con gli STESSI
selettori, apriva il pannello nel 95% dei casi (19/20). Quindi non e' il
selettore: e' qualcosa che il codice della Fase A fa diversamente dal PoC.

Due candidati, e questo script li separa invece di sceglierne uno a caso:
  A. il click non apre la chat (human_click su un handle da evaluate_handle
     contro locator.nth(i).click() del PoC-4);
  B. la chat si apre ma l'header non e' ancora leggibile quando lo interroghiamo
     (querySelector immediato contro wait_for(state='visible') del PoC-4).

Per ogni riga prova ENTRAMBI i click e dumpa lo stato del DOM dopo ciascuno:
esistenza di #main, dell'header, cosa contiene, e l'URL (che su WhatsApp cambia
quando una chat si apre davvero). SOLA LETTURA: nessun invio, nessun testo
digitato.

Uso (dal folder backend):
    ./venv/Scripts/python.exe probe_wa_click.py
"""
import asyncio
import json
import os
import sys

WORKTREE = r"D:\BOT OUTBOUND\.worktrees\wa-fase4-autodiscover\backend"
sys.path.insert(0, WORKTREE)
sys.path.insert(1, os.path.dirname(os.path.abspath(__file__)))

NUMERO = "8c578c08-6659-43fa-9840-f55e88e220fc"

# Lo stato del DOM che conta per capire se una chat e' aperta.
JS_STATO = """() => {
  const main = document.querySelector('#main');
  const header = document.querySelector("#main header, header[data-testid='conversation-header']");
  const qualsiasiHeader = document.querySelectorAll('header').length;
  return {
    url: location.href,
    main_esiste: !!main,
    header_esiste: !!header,
    header_testo: header ? (header.innerText || '').slice(0, 120) : null,
    header_visibile: header ? !!(header.offsetWidth || header.offsetHeight) : null,
    quanti_header_in_pagina: qualsiasiHeader,
    // Se #main non c'e', cosa occupa la colonna di destra?
    testid_presenti: [...document.querySelectorAll('[data-testid]')]
      .map(e => e.getAttribute('data-testid'))
      .filter((v, i, a) => a.indexOf(v) === i).slice(0, 25),
  };
}"""


async def main():
    from app.browser import human_input
    from app.browser import whatsapp_selectors as sel
    from app.browser.whatsapp_page import WhatsAppWebPage
    from app.database import AsyncSessionLocal
    from app.services.wa_session import (
        WHATSAPP_WEB_URL, _open_wa_browser, _wa_number_or_raise,
    )

    async with AsyncSessionLocal() as db:
        numero = await _wa_number_or_raise(db, NUMERO)
        proxy_url = numero.proxy_url

    referto = {"prima_di_tutto": None, "prove": []}

    async with _open_wa_browser(NUMERO, headless=False, proxy_url=proxy_url) as context:
        page = await context.new_page()
        await page.goto(WHATSAPP_WEB_URL, wait_until="commit", timeout=120_000)
        for attesa_s in (3, 5, 10, 20):
            await page.wait_for_timeout(attesa_s * 1000)
            if await page.evaluate("() => !!document.querySelector('#pane-side')"):
                break

        pom = WhatsAppWebPage(page)
        stato = await pom.session_state()
        print(f"session_state={stato}", file=sys.stderr)
        if stato != "logged_in":
            print(json.dumps({"errore": f"sessione {stato}"}))
            return

        # Stato PRIMA di qualunque click: e' il riferimento. Se #main esiste
        # gia' adesso, allora "header assente" non significa "chat non aperta".
        referto["prima_di_tutto"] = await page.evaluate(JS_STATO)
        print("\n=== PRIMA DI OGNI CLICK ===", file=sys.stderr)
        print(json.dumps(referto["prima_di_tutto"], ensure_ascii=True, indent=2),
              file=sys.stderr)

        # ---- PROVA A: il click del PoC-4 (locator.nth(i).click) ----
        prova_a = {"metodo": "locator.nth(0).click()  [come PoC-4]"}
        try:
            await page.locator("[role='row']").nth(1).click(timeout=5000)
            await page.wait_for_timeout(1500)
            prova_a["dopo"] = await page.evaluate(JS_STATO)
        except Exception as exc:
            prova_a["errore"] = f"{type(exc).__name__}: {exc}"
        referto["prove"].append(prova_a)
        print("\n=== PROVA A (click del PoC-4) ===", file=sys.stderr)
        print(json.dumps(prova_a, ensure_ascii=True, indent=2)[:1500], file=sys.stderr)

        # Torna a uno stato neutro prima della seconda prova.
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(800)

        # ---- PROVA B: il click della Fase A (human_click su evaluate_handle) ----
        prova_b = {"metodo": "human_click(evaluate_handle)  [come Fase A]"}
        try:
            handle = await page.evaluate_handle(
                """(args) => {
                    const pane = document.querySelector(args.pane);
                    if (!pane) return null;
                    const rows = Array.from(pane.querySelectorAll(args.row))
                      .filter(r => r.querySelector(args.rowMarker) || r.matches(args.yourself));
                    return rows[args.idx] || null;
                }""",
                {"pane": sel.CHATLIST[0], "row": sel.ROW, "rowMarker": sel.ROW_MARKER,
                 "yourself": sel.YOURSELF_ROW, "idx": 2},
            )
            elemento = handle.as_element()
            prova_b["handle_risolto"] = elemento is not None
            if elemento is not None:
                await human_input.human_click(page, elemento)
                await page.wait_for_timeout(1500)
                prova_b["dopo"] = await page.evaluate(JS_STATO)
        except Exception as exc:
            prova_b["errore"] = f"{type(exc).__name__}: {exc}"
        referto["prove"].append(prova_b)
        print("\n=== PROVA B (click della Fase A) ===", file=sys.stderr)
        print(json.dumps(prova_b, ensure_ascii=True, indent=2)[:1500], file=sys.stderr)

        # ---- PROVA C: l'header compare piu' tardi? ----
        # Se il click B ha aperto la chat ma l'header tardava, aspettando ancora
        # deve comparire. Distingue "click fallito" da "letto troppo presto".
        prova_c = {"metodo": "attesa prolungata dopo il click B"}
        for attesa_s in (1, 2, 4, 8):
            await page.wait_for_timeout(attesa_s * 1000)
            stato_ora = await page.evaluate(JS_STATO)
            if stato_ora["header_esiste"]:
                prova_c["comparso_dopo_s"] = attesa_s
                prova_c["dopo"] = stato_ora
                break
        else:
            prova_c["comparso_dopo_s"] = None
            prova_c["dopo"] = await page.evaluate(JS_STATO)
        referto["prove"].append(prova_c)
        print("\n=== PROVA C (l'header compare piu' tardi?) ===", file=sys.stderr)
        print(json.dumps(prova_c, ensure_ascii=True, indent=2)[:1200], file=sys.stderr)

        out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "scripts", "poc_wa", "artifacts", "probe_click.json")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(referto, f, ensure_ascii=False, indent=2)
        print(f"\n[OK] referto in {out}", file=sys.stderr)


for flusso in (sys.stdout, sys.stderr):
    try:
        flusso.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

asyncio.run(main())
