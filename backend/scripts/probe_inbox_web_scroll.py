"""Quali segnali distinguono "sta caricando" da "fine lista"?

Scorre la lista chat qualche volta e a ogni giro registra:
  - quante righe ci sono
  - l'altezza totale del contenitore scrollabile
  - la posizione di scroll (siamo al fondo?)
  - se c'e' un indicatore di caricamento (spinner / skeleton / aria-busy)

Sola lettura, nessuna chat aperta, nessuna scrittura.
"""
import asyncio, sys
sys.path.insert(0, r"D:\BOT OUTBOUND\backend")

from sqlalchemy import select
from app.browser.context_manager import BrowserSession
from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount

ACCOUNT = "claudio.abbigliamentovincente"
GIRI = 10

JS_STATO = """() => {
    // Il contenitore scrollabile della lista: il piu' alto elemento con overflow
    // nella colonna sinistra.
    const tutti = [...document.querySelectorAll('div')];
    let box = null, best = 0;
    for (const e of tutti) {
        const r = e.getBoundingClientRect();
        if (r.left > 700 || r.width < 200 || r.height < 300) continue;
        const st = getComputedStyle(e);
        if (!/auto|scroll/.test(st.overflowY)) continue;
        if (e.scrollHeight > best) { best = e.scrollHeight; box = e; }
    }
    const righe = tutti.filter(e => { const r = e.getBoundingClientRect();
        return r.left < 660 && r.top > 150 && r.height > 50 && r.height < 130 && r.width > 250; }).length;

    // Candidati indicatore di caricamento
    const spinner = document.querySelectorAll('svg[aria-label*="Carica" i], svg[aria-label*="Load" i], [role="progressbar"], [aria-busy="true"]').length;
    // Gli scheletri di IG sono spesso div animati senza testo in fondo alla lista
    const animati = [...document.querySelectorAll('div')].filter(e => {
        const st = getComputedStyle(e);
        return st.animationName && st.animationName !== 'none' && e.getBoundingClientRect().left < 660;
    }).length;

    return {
        righe,
        scrollHeight: box ? box.scrollHeight : null,
        scrollTop: box ? Math.round(box.scrollTop) : null,
        clientHeight: box ? box.clientHeight : null,
        alFondo: box ? (box.scrollHeight - box.scrollTop - box.clientHeight) < 50 : null,
        spinner, animati,
    };
}"""

JS_SCROLL = """() => {
    const tutti = [...document.querySelectorAll('div')];
    let box = null, best = 0;
    for (const e of tutti) {
        const r = e.getBoundingClientRect();
        if (r.left > 700 || r.width < 200 || r.height < 300) continue;
        const st = getComputedStyle(e);
        if (!/auto|scroll/.test(st.overflowY)) continue;
        if (e.scrollHeight > best) { best = e.scrollHeight; box = e; }
    }
    if (!box) return false;
    box.scrollTop = box.scrollTop + box.clientHeight * 0.8;
    return true;
}"""


async def main():
    async with AsyncSessionLocal() as db:
        acct = (await db.execute(
            select(InstagramAccount).where(InstagramAccount.username == ACCOUNT)
        )).scalar_one_or_none()
    if acct is None:
        print("[X] account non trovato")
        return
    session = BrowserSession(acct.id)
    try:
        await session.open()
    except Exception as e:
        print(f"[X] browser: {type(e).__name__}: {e}")
        return
    try:
        ctx = session.context
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.instagram.com/direct/inbox/", wait_until="domcontentloaded")
        await page.wait_for_timeout(10000)

        print(f"{'giro':>4} {'righe':>6} {'altezza':>8} {'scrollTop':>10} {'alFondo':>8} {'spinner':>8} {'animati':>8}")
        print("-" * 60)
        s = await page.evaluate(JS_STATO)
        print(f"{'iniz':>4} {s['righe']:>6} {str(s['scrollHeight']):>8} {str(s['scrollTop']):>10} "
              f"{str(s['alFondo']):>8} {s['spinner']:>8} {s['animati']:>8}")

        for g in range(1, GIRI + 1):
            ok = await page.evaluate(JS_SCROLL)
            if not ok:
                print("  [X] contenitore scrollabile non trovato")
                break
            # SUBITO dopo lo scroll: qui dovrebbe vedersi il caricamento in corso
            await page.wait_for_timeout(250)
            subito = await page.evaluate(JS_STATO)
            # dopo un attimo: dovrebbe essere arrivato altro contenuto
            await page.wait_for_timeout(2500)
            dopo = await page.evaluate(JS_STATO)
            print(f"{g:>4} {dopo['righe']:>6} {str(dopo['scrollHeight']):>8} {str(dopo['scrollTop']):>10} "
                  f"{str(dopo['alFondo']):>8} {dopo['spinner']:>8} {dopo['animati']:>8}"
                  f"   (subito dopo lo scroll: spinner={subito['spinner']} animati={subito['animati']} "
                  f"altezza={subito['scrollHeight']})")
    finally:
        await session.close()
        print("[OK] chiuso")


asyncio.run(main())
