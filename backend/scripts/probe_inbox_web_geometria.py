# backend/scripts/probe_inbox_web_geometria.py
"""Probe diagnostico: la geometria assunta dal motore inbox-browser e' vera?

Nato dal collaudo del 10/08: `apri_riga` fallisce sistematicamente la verifica
post-click (`aperto None`, `aperto 'Top'`), alcune righe hanno nome vuoto, e la
lista viene dichiarata "fine" dopo pochi scroll pur avendo migliaia di chat.

Tutti e tre i sintomi puntano alle costanti geometriche di pagina.py
(`left < 660` per le righe, `left > 660` per header/href) e alla scelta del
contenitore scrollabile. Questo probe le MISURA invece di ipotizzarle.

Sola lettura: apre UNA chat gia' letta (stesso `human_click` del motore, stesso
effetto di una lettura umana), nessuna scrittura su DB, nessuna API mobile.

Uso (dal folder backend, con la sessione del bot ferma):
    ./venv/Scripts/python.exe scripts/probe_inbox_web_geometria.py primero_adv3
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from app.browser import human_input
from app.browser.context_manager import BrowserSession
from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "probe_inbox_geometria.json")

JS_VIEWPORT = """() => ({w: window.innerWidth, h: window.innerHeight,
                         dpr: window.devicePixelRatio, url: location.href, title: document.title})"""

# Tutti i contenitori scrollabili, senza il filtro del motore: serve vedere
# QUALE il motore sta scegliendo e quale sarebbe quello giusto.
JS_CONTENITORI = """() => [...document.querySelectorAll('div')].map(e => {
    const r = e.getBoundingClientRect();
    const st = getComputedStyle(e);
    if (!/auto|scroll/.test(st.overflowY)) return null;
    if (e.scrollHeight <= e.clientHeight + 10) return null;
    if (r.height < 100) return null;
    return {left: Math.round(r.left), top: Math.round(r.top),
            w: Math.round(r.width), h: Math.round(r.height),
            scrollHeight: e.scrollHeight, clientHeight: e.clientHeight,
            scrollTop: Math.round(e.scrollTop),
            testo: (e.innerText || '').slice(0, 60).replace(/\\n/g, ' | ')};
}).filter(Boolean)"""

# Righe candidate SENZA il vincolo left<660: si vuole vedere dove stanno davvero.
JS_RIGHE = """() => [...document.querySelectorAll('div[role="button"], div[tabindex="0"], a')]
    .filter(e => { const r = e.getBoundingClientRect();
      return r.height > 50 && r.height < 130 && r.width > 250; })
    .slice(0, 40)
    .map((e, i) => { const r = e.getBoundingClientRect();
      return {i, left: Math.round(r.left), right: Math.round(r.right),
              top: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height),
              tag: e.tagName, href: e.getAttribute('href'),
              testo: (e.innerText || '').split('\\n').slice(0, 3).join(' | ')}; })"""

# Tutti i nodi foglia della fascia alta, con la loro X: da qui si vede se il nome
# dell'header del thread cade sopra o sotto la soglia 660 usata dal motore.
JS_FASCIA_ALTA = """() => [...document.querySelectorAll('span, div, h1, h2')]
    .filter(e => { const r = e.getBoundingClientRect();
      return e.children.length === 0 && (e.textContent || '').trim().length > 1
             && r.top < 200 && r.width > 0 && r.height > 0; })
    .map(e => { const r = e.getBoundingClientRect();
      return {left: Math.round(r.left), top: Math.round(r.top),
              testo: e.textContent.trim().slice(0, 40)}; })"""

JS_HREF_DESTRA = """() => [...document.querySelectorAll('a[href^="/"]')]
    .map(e => { const r = e.getBoundingClientRect();
      return {left: Math.round(r.left), top: Math.round(r.top), href: e.getAttribute('href')}; })
    .filter(x => x.href.split('/').filter(Boolean).length === 1)"""

# Stesso identico filtro del motore, per confronto diretto.
JS_HEADER_MOTORE = """() => {
    const t = [...document.querySelectorAll('span, div')]
      .filter(e => { const r = e.getBoundingClientRect();
        return r.left > 660 && r.top < 130 && e.children.length === 0
               && e.textContent.trim().length > 1; })
      .map(e => e.textContent.trim());
    return [...new Set(t)];
}"""

JS_CONTENITORE_MOTORE = """() => {
    let box = null, best = 0;
    for (const e of document.querySelectorAll('div')) {
        const r = e.getBoundingClientRect();
        if (r.left > 700 || r.width < 200 || r.height < 300) continue;
        if (!/auto|scroll/.test(getComputedStyle(e).overflowY)) continue;
        if (e.scrollHeight > best) { best = e.scrollHeight; box = e; }
    }
    if (!box) return null;
    const r = box.getBoundingClientRect();
    return {left: Math.round(r.left), top: Math.round(r.top), w: Math.round(r.width),
            altezza: box.scrollHeight, top_scroll: Math.round(box.scrollTop),
            visibile: box.clientHeight,
            alFondo: (box.scrollHeight - box.scrollTop - box.clientHeight) < 50,
            testo: (box.innerText || '').slice(0, 60).replace(/\\n/g, ' | ')};
}"""

JS_SCROLL_MOTORE = """(f) => {
    let box = null, best = 0;
    for (const e of document.querySelectorAll('div')) {
        const r = e.getBoundingClientRect();
        if (r.left > 700 || r.width < 200 || r.height < 300) continue;
        if (!/auto|scroll/.test(getComputedStyle(e).overflowY)) continue;
        if (e.scrollHeight > best) { best = e.scrollHeight; box = e; }
    }
    if (box) box.scrollTop += box.clientHeight * f;
}"""


def p(s):
    return str(s).encode("ascii", "replace").decode("ascii")


async def main():
    username = sys.argv[1] if len(sys.argv) > 1 else "primero_adv3"
    async with AsyncSessionLocal() as db:
        acct = (await db.execute(select(InstagramAccount).where(
            InstagramAccount.username == username))).scalar_one_or_none()
    if acct is None:
        print(f"[X] account {username} non trovato")
        return

    out = {"account": username}
    session = BrowserSession(acct.id)
    await session.open()
    try:
        ctx = session.context
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.instagram.com/direct/inbox/",
                        wait_until="commit", timeout=60000)
        await page.wait_for_timeout(8000)

        out["viewport"] = await page.evaluate(JS_VIEWPORT)
        print("\n=== VIEWPORT ===")
        print(" ", p(out["viewport"]))

        out["contenitori"] = await page.evaluate(JS_CONTENITORI)
        print("\n=== CONTENITORI SCROLLABILI (tutti) ===")
        for c in out["contenitori"]:
            print(f"  left={c['left']:>5} top={c['top']:>4} w={c['w']:>4} h={c['h']:>4} "
                  f"scrollH={c['scrollHeight']:>6} clientH={c['clientHeight']:>5} | {p(c['testo'])}")

        out["contenitore_motore"] = await page.evaluate(JS_CONTENITORE_MOTORE)
        print("\n=== CONTENITORE SCELTO DAL MOTORE ===")
        print(" ", p(out["contenitore_motore"]))

        out["righe"] = await page.evaluate(JS_RIGHE)
        print("\n=== RIGHE CANDIDATE (senza vincolo left<660) ===")
        for r in out["righe"]:
            marca = "OK " if r["left"] < 660 and r["top"] > 150 else "ESCL"
            print(f"  [{r['i']:>2}] {marca} left={r['left']:>5} right={r['right']:>5} "
                  f"top={r['top']:>4} h={r['h']:>3} {r['tag']:<4} | {p(r['testo'])[:70]}")

        out["fascia_alta_prima"] = await page.evaluate(JS_FASCIA_ALTA)
        out["header_motore_prima"] = await page.evaluate(JS_HEADER_MOTORE)
        print("\n=== HEADER SECONDO IL MOTORE, PRIMA DEL CLICK ===")
        print(" ", p(out["header_motore_prima"]))

        # --- click su una riga gia' letta (font-weight 400) ---
        idx = await page.evaluate("""() => {
            const righe = [...document.querySelectorAll('div[role="button"], div[tabindex="0"], a')]
              .filter(e => { const r = e.getBoundingClientRect();
                return r.left < 660 && r.top > 150 && r.height > 50 && r.height < 130 && r.width > 250; });
            for (let i = 0; i < righe.length; i++) {
                const grassetto = [...righe[i].querySelectorAll('span, div')]
                  .filter(n => n.children.length === 0 && n.textContent.trim().length > 0)
                  .some(n => parseInt(getComputedStyle(n).fontWeight, 10) >= 600);
                if (!grassetto && (righe[i].innerText || '').trim()) return i;
            }
            return -1;
        }""")
        out["indice_cliccato"] = idx
        print(f"\n=== CLICK su riga letta indice {idx} ===")
        if idx >= 0:
            handle = await page.evaluate_handle(
                """(i) => [...document.querySelectorAll('div[role="button"], div[tabindex="0"], a')]
                     .filter(e => { const r = e.getBoundingClientRect();
                       return r.left < 660 && r.top > 150 && r.height > 50 && r.height < 130 && r.width > 250; })[i]""",
                idx)
            el = handle.as_element()
            out["nome_cliccato"] = (await page.evaluate(
                "(e) => (e.innerText || '').split('\\n')[0]", el))
            print("  nome atteso:", p(out["nome_cliccato"]))
            await human_input.human_click(page, el)
            await page.wait_for_timeout(3000)

            out["viewport_dopo"] = await page.evaluate(JS_VIEWPORT)
            out["fascia_alta_dopo"] = await page.evaluate(JS_FASCIA_ALTA)
            out["header_motore_dopo"] = await page.evaluate(JS_HEADER_MOTORE)
            out["href_dopo"] = await page.evaluate(JS_HREF_DESTRA)

            print("  title:", p(out["viewport_dopo"]["title"]))
            print("  url:  ", p(out["viewport_dopo"]["url"]))
            print("\n  --- fascia alta (top<200), ORDINE DOCUMENTO, con X ---")
            for n in out["fascia_alta_dopo"]:
                print(f"    left={n['left']:>5} top={n['top']:>4} | {p(n['testo'])}")
            print("\n  --- header secondo il motore (left>660, top<130) ---")
            print("   ", p(out["header_motore_dopo"]))
            print("\n  --- href a segmento singolo, con X ---")
            for h in out["href_dopo"]:
                print(f"    left={h['left']:>5} top={h['top']:>4} | {p(h['href'])}")

        # --- 6 giri di scroll come li fa il motore: l'altezza cresce davvero? ---
        print("\n=== SCROLL (6 passi da 0.7 schermata, come il motore) ===")
        storia = []
        for giro in range(6):
            await page.evaluate(JS_SCROLL_MOTORE, 0.7)
            await page.wait_for_timeout(1500)
            st = await page.evaluate(JS_CONTENITORE_MOTORE)
            storia.append(st)
            if st is None:
                print(f"  giro {giro}: contenitore NON trovato")
                continue
            print(f"  giro {giro}: scrollHeight={st['altezza']:>6} scrollTop={st['top_scroll']:>6} "
                  f"clientH={st['visibile']:>5} alFondo={st['alFondo']}")
        out["storia_scroll"] = storia

        out["righe_dopo_scroll"] = await page.evaluate(JS_RIGHE)
        print("\n=== RIGHE DOPO LO SCROLL (prime 12) ===")
        for r in out["righe_dopo_scroll"][:12]:
            print(f"  [{r['i']:>2}] left={r['left']:>5} top={r['top']:>4} | {p(r['testo'])[:70]}")

    finally:
        await session.close()
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"\n[OK] chiuso. Dump completo: {OUT}")


asyncio.run(main())
