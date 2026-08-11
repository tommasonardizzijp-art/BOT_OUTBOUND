"""PROBE 2: isola il confine fra il gate di sincronizzazione e l'apertura chat.

Il probe 1 ha mostrato che, prima di qualunque click, il DOM aveva
drawer-fullscreen/drawer-left aperti (il pannello Impostazioni) e che
locator("[role='row']") andava in timeout. Ma ha anche mostrato che human_click
apriva comunque la chat: quindi "drawer aperto" da solo non spiega i 4
fallimenti del collaudo.

Qui si replica la sequenza VERA del giro -- leggi_percentuale (che apre e
richiude Impostazioni) seguito da apri_e_leggi -- e si fotografa il DOM in
mezzo, per vedere se il gate lascia la UI in uno stato che l'apertura poi non
gestisce. Sola lettura.
"""
import asyncio
import json
import os
import sys

WORKTREE = r"D:\BOT OUTBOUND\.worktrees\wa-fase4-autodiscover\backend"
sys.path.insert(0, WORKTREE)
sys.path.insert(1, os.path.dirname(os.path.abspath(__file__)))

NUMERO = "8c578c08-6659-43fa-9840-f55e88e220fc"

JS_STATO = """() => {
  const drawerAperti = [...document.querySelectorAll('[data-testid]')]
    .map(e => e.getAttribute('data-testid'))
    .filter(t => t && t.startsWith('drawer'));
  const header = document.querySelector("#main header, header[data-testid='conversation-header']");
  return {
    drawer_aperti: [...new Set(drawerAperti)],
    main_esiste: !!document.querySelector('#main'),
    header_testo: header ? (header.innerText || '').split('\\n')[0] : null,
    pane_side: !!document.querySelector('#pane-side'),
    righe_visibili: document.querySelectorAll("#pane-side [role='row']").length,
  };
}"""


async def main():
    from app.browser.whatsapp_page import WhatsAppWebPage
    from app.database import AsyncSessionLocal
    from app.services.wa_discover import pannello, sidebar
    from app.services.wa_discover.sincronizzazione import leggi_percentuale
    from app.services.wa_session import (
        WHATSAPP_WEB_URL, _open_wa_browser, _wa_number_or_raise,
    )

    async with AsyncSessionLocal() as db:
        numero = await _wa_number_or_raise(db, NUMERO)
        proxy_url = numero.proxy_url

    referto = {}
    async with _open_wa_browser(NUMERO, headless=False, proxy_url=proxy_url) as context:
        page = await context.new_page()
        await page.goto(WHATSAPP_WEB_URL, wait_until="commit", timeout=120_000)
        for attesa_s in (3, 5, 10, 20):
            await page.wait_for_timeout(attesa_s * 1000)
            if await page.evaluate("() => !!document.querySelector('#pane-side')"):
                break

        pom = WhatsAppWebPage(page)
        if await pom.session_state() != "logged_in":
            print("sessione non loggata", file=sys.stderr)
            return

        referto["1_appena_caricato"] = await page.evaluate(JS_STATO)

        # Il gate, esattamente come lo chiama l'orchestratore.
        percentuale = await leggi_percentuale(page)
        referto["2_dopo_il_gate"] = await page.evaluate(JS_STATO)
        referto["2_percentuale_letta"] = percentuale

        # Le righe che l'orchestratore vedrebbe adesso.
        righe = await sidebar.scan_sidebar(page)
        referto["3_righe_viste"] = len(righe)
        referto["3_primi_titoli"] = [r["titolo"][:30] for r in righe[:5]]

        # L'apertura vera, sulla prima riga che NON ha il numero come titolo.
        candidata = next((r for r in righe if not r["titolo_e_numero"]), None)
        if candidata:
            referto["4_titolo_atteso"] = candidata["titolo"][:40]
            esito = await pannello.apri_e_leggi(page, candidata["titolo"])
            referto["4_esito"] = esito.esito
            referto["4_numero_letto"] = bool(esito.numero)
            referto["4_stato_dom"] = await page.evaluate(JS_STATO)

        print(json.dumps(referto, ensure_ascii=True, indent=2), file=sys.stderr)
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "scripts", "poc_wa", "artifacts", "probe_gate.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(referto, f, ensure_ascii=False, indent=2)
        print(f"\n[OK] {out}", file=sys.stderr)


for flusso in (sys.stdout, sys.stderr):
    try:
        flusso.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

asyncio.run(main())
