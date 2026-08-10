# backend/scripts/probe_inbox_web_nonlette.py
"""Come si riconosce una chat NON LETTA dalla lista, senza aprirla?

La spec decide di aprire solo le chat gia' lette, per non bruciare il badge dei
non letti. Serve un segnale affidabile. Ipotesi da verificare: pallino colorato,
nome in grassetto (font-weight), aria-label dedicata.

Sola lettura, nessuna chat aperta.
"""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from app.browser.context_manager import BrowserSession
from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount

ACCOUNT = "claudio.abbigliamentovincente"

JS = """() => {
    const righe = [...document.querySelectorAll('div[role="button"], div[tabindex="0"], a')]
      .filter(e => { const r = e.getBoundingClientRect();
        return r.left < 660 && r.top > 200 && r.height > 50 && r.height < 130 && r.width > 250; });
    return righe.slice(0, 15).map(e => {
        const testo = e.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
        // peso del font sui nodi di testo: un non letto e' spesso in grassetto
        const pesi = [...e.querySelectorAll('span, div')]
          .filter(n => n.children.length === 0 && n.textContent.trim())
          .map(n => getComputedStyle(n).fontWeight);
        // pallini: elementi piccoli e tondi con background pieno
        const pallini = [...e.querySelectorAll('div, span')].filter(n => {
            const r = n.getBoundingClientRect(); const st = getComputedStyle(n);
            return r.width > 4 && r.width < 16 && Math.abs(r.width - r.height) < 3
                   && parseFloat(st.borderRadius) > 0
                   && st.backgroundColor !== 'rgba(0, 0, 0, 0)';
        }).length;
        const aria = e.getAttribute('aria-label');
        return {nome: testo[0] || null, pesi: [...new Set(pesi)], pallini, aria};
    });
}"""


def p(s):
    return str(s).encode("ascii", "replace").decode("ascii")


async def main():
    async with AsyncSessionLocal() as db:
        acct = (await db.execute(select(InstagramAccount).where(
            InstagramAccount.username == ACCOUNT))).scalar_one_or_none()
    if acct is None:
        print(f"[X] account {ACCOUNT} non trovato")
        return
    session = BrowserSession(acct.id)
    await session.open()
    try:
        ctx = session.context
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.instagram.com/direct/inbox/", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(10000)
        for r in await page.evaluate(JS):
            print(f"  pallini={r['pallini']} pesi={r['pesi']} aria={p(r['aria'])[:40]}  {p(r['nome'])[:50]}")
    finally:
        await session.close()
        print("[OK] chiuso — nessuna chat aperta")


asyncio.run(main())
