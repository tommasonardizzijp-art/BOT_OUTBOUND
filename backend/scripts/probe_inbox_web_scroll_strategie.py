# backend/scripts/probe_inbox_web_scroll_strategie.py
"""Quale gesto scrolla DAVVERO la lista chat dell'inbox web?

Misurato il 10/08 col probe geometria: `box.scrollTop += box.clientHeight * f`
(quello che usa `pagina.scorri`) lascia `scrollTop` a 1 per sei giri di fila —
la lista non avanza, IG non carica altro, e il motore dichiara "piantato".

Questo probe confronta quattro gesti sullo stesso contenitore, ripartendo ogni
volta dalla cima, e per ognuno misura: scrollTop raggiunto, crescita di
scrollHeight (= caricamento riuscito), righe nel DOM, prima riga visibile. In
piu' cerca QUALE elemento della pagina ha scrollTop > 0 dopo il gesto: se non e'
quello che il motore sceglie, il contenitore e' sbagliato in partenza.

Sola lettura: nessuna chat aperta, nessuna scrittura su DB, nessuna API mobile.

Uso (dal folder backend, con la sessione del bot ferma):
    ./venv/Scripts/python.exe scripts/probe_inbox_web_scroll_strategie.py primero_adv3
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from app.browser.context_manager import BrowserSession
from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "probe_inbox_scroll.json")

# Il contenitore come lo sceglie il motore (pagina._JS_CONTENITORE).
JS_TROVA = """() => {
    let box = null, best = 0;
    for (const e of document.querySelectorAll('div')) {
        const r = e.getBoundingClientRect();
        if (r.left > 700 || r.width < 200 || r.height < 300) continue;
        if (!/auto|scroll/.test(getComputedStyle(e).overflowY)) continue;
        if (e.scrollHeight > best) { best = e.scrollHeight; box = e; }
    }
    window.__box = box;
    if (!box) return null;
    const r = box.getBoundingClientRect();
    return {left: Math.round(r.left), top: Math.round(r.top), w: Math.round(r.width),
            h: Math.round(r.height), scrollHeight: box.scrollHeight,
            clientHeight: box.clientHeight, scrollTop: Math.round(box.scrollTop)};
}"""

JS_STATO = """() => {
    const b = window.__box;
    if (!b) return null;
    const righe = [...document.querySelectorAll('div[role="button"], div[tabindex="0"], a')]
      .filter(e => { const r = e.getBoundingClientRect();
        return r.left < 660 && r.top > 150 && r.height > 50 && r.height < 130 && r.width > 250; });
    return {scrollTop: Math.round(b.scrollTop), scrollHeight: b.scrollHeight,
            clientHeight: b.clientHeight, nRighe: righe.length,
            prima: righe.length ? (righe[0].innerText || '').split('\\n')[0] : null,
            ultima: righe.length ? (righe[righe.length-1].innerText || '').split('\\n')[0] : null};
}"""

# Chi ha davvero scrollato: ogni elemento con scrollTop > 0.
JS_CHI_SCROLLA = """() => [...document.querySelectorAll('*')]
    .filter(e => e.scrollTop > 0)
    .map(e => { const r = e.getBoundingClientRect();
      return {tag: e.tagName, cls: (e.className || '').toString().slice(0, 30),
              left: Math.round(r.left), top: Math.round(r.top),
              w: Math.round(r.width), h: Math.round(r.height),
              scrollTop: Math.round(e.scrollTop), scrollHeight: e.scrollHeight,
              eIlBox: e === window.__box}; })"""

JS_RESET = """() => { if (window.__box) window.__box.scrollTop = 0; }"""


def p(s):
    return str(s).encode("ascii", "replace").decode("ascii")


async def prova(page, nome, gesto, passi=5):
    await page.evaluate(JS_RESET)
    await page.wait_for_timeout(1200)
    partenza = await page.evaluate(JS_STATO)
    print(f"\n=== {nome} ===")
    print(f"  partenza: scrollTop={partenza['scrollTop']} scrollHeight={partenza['scrollHeight']} "
          f"righe={partenza['nRighe']} prima={p(partenza['prima'])[:24]}")
    storia = [partenza]
    for i in range(passi):
        await gesto(page)
        await page.wait_for_timeout(1500)
        st = await page.evaluate(JS_STATO)
        storia.append(st)
        print(f"  passo {i}: scrollTop={st['scrollTop']:>6} scrollHeight={st['scrollHeight']:>6} "
              f"righe={st['nRighe']:>3} prima={p(st['prima'])[:20]:<20} ultima={p(st['ultima'])[:20]}")
    chi = await page.evaluate(JS_CHI_SCROLLA)
    print("  chi ha scrollTop>0:")
    for c in chi[:6]:
        print(f"    {c['tag']:<5} left={c['left']:>5} top={c['top']:>4} w={c['w']:>4} h={c['h']:>4} "
              f"scrollTop={c['scrollTop']:>6} scrollHeight={c['scrollHeight']:>6} eIlBox={c['eIlBox']}")
    return {"storia": storia, "chi_scrolla": chi}


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

        box = await page.evaluate(JS_TROVA)
        out["box"] = box
        print("=== CONTENITORE SCELTO DAL MOTORE ===")
        print(" ", p(box))
        if box is None:
            return

        # centro della lista, per hover e wheel
        cx = box["left"] + box["w"] // 2
        cy = box["top"] + box["h"] // 2

        async def g_scrolltop(page):
            await page.evaluate("() => { window.__box.scrollTop += window.__box.clientHeight * 0.7; }")

        async def g_scrollby(page):
            await page.evaluate(
                "() => window.__box.scrollBy({top: window.__box.clientHeight * 0.7, behavior: 'smooth'})")

        async def g_wheel(page):
            await page.mouse.move(cx, cy)
            await page.mouse.wheel(0, 300)

        async def g_pagedown(page):
            # Solo hover + tasto: nessun click, per non aprire chat ne' tab nuove.
            await page.mouse.move(cx, cy)
            await page.keyboard.press("PageDown")

        out["scrollTop_diretto"] = await prova(page, "1. scrollTop += (quello del motore)", g_scrolltop)
        out["scrollBy_smooth"] = await prova(page, "2. scrollBy smooth", g_scrollby)
        out["mouse_wheel"] = await prova(page, "3. mouse.wheel 300px", g_wheel)
        out["pagedown"] = await prova(page, "4. PageDown da tastiera", g_pagedown)

        # wheel a raffica: la lista virtualizzata carica davvero in fondo?
        print("\n=== 5. mouse.wheel a raffica (20 x 300px) ===")
        await page.evaluate(JS_RESET)
        await page.wait_for_timeout(1000)
        raffica = []
        for i in range(20):
            await page.mouse.move(cx, cy)
            await page.mouse.wheel(0, 300)
            await page.wait_for_timeout(700)
            st = await page.evaluate(JS_STATO)
            raffica.append(st)
            if i % 4 == 0 or i == 19:
                print(f"  giro {i:>2}: scrollTop={st['scrollTop']:>6} scrollHeight={st['scrollHeight']:>6} "
                      f"righe={st['nRighe']:>3} ultima={p(st['ultima'])[:26]}")
        out["raffica_wheel"] = raffica

    finally:
        await session.close()
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"\n[OK] chiuso. Dump: {OUT}")


asyncio.run(main())
